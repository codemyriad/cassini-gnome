# SPDX-License-Identifier: GPL-3.0-or-later
"""One FIFO per app, at most one speech child; no engine import in GTK."""

import contextlib
import importlib.util
import logging
import os
import platform
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

from .library import LibraryScanner, library_root, meeting_id, track_identity, track_rows
from .library_edits import mutation, transaction
from .meeting_store import atomic_json, read_metadata, transcript_snapshot, update_metadata
from .strings import text
from .transcript_publish import TranscriptPublisher
from .whistle_client import WhistleClient
from .whistle_worker import onnxruntime_dir

LOG = logging.getLogger(__name__)
REFERENCE = dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
RECOGNITION_CACHE_FILE = ".recognition-cache.json"
RECOGNITION_CACHE_FORMAT = "cassini.recognition-cache.v1"


def _read_recognition_cache_entries(directory, model):
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        from .meeting_store import regular, strict_json

        with regular(descriptor, RECOGNITION_CACHE_FILE) as (stream, _info):
            raw = stream.read(16 * 1024 * 1024 + 1)
    except OSError:
        return {}
    finally:
        os.close(descriptor)
    if len(raw) > 16 * 1024 * 1024:
        return {}
    try:
        value = strict_json(raw)
    except ValueError:
        return {}
    if (
        not isinstance(value, dict)
        or value.get("format") != RECOGNITION_CACHE_FORMAT
        or value.get("model") != model
        or not isinstance(value.get("segments"), dict)
    ):
        return {}
    return dict(value["segments"])


def bundle_path():
    return Path(os.environ.get("CASSINI_WHISTLE_BUNDLE", Path(__file__).resolve().parent.parent / "whistle"))


def unavailable_reason(bundle=None):
    """Why this computer cannot write meetings down, or None when it can.

    Cheap and import-free: GTK must never import NumPy, ONNX Runtime or the
    engine, so dependencies are only located, not loaded: NumPy in this
    Python, ONNX Runtime in the app's private directory (packages) or else in
    this Python. The worker still verifies every bundle hash, and every file
    of the private ONNX Runtime, before it loads anything.
    """
    if sys.platform != "linux" or platform.machine() != "x86_64":
        return f"the offline engine runs only on Linux x86_64, not {sys.platform} {platform.machine()}"
    libc = platform.libc_ver()[0]
    if libc != "glibc":
        return f"the offline engine needs glibc, not {libc or 'this C library'}"
    bundle = Path(bundle) if bundle else bundle_path()
    if not (bundle / "pins.json").is_file():
        return f"the offline engine is not installed (no {bundle / 'pins.json'})"
    missing = ["numpy"] if importlib.util.find_spec("numpy") is None else []
    if onnxruntime_dir() is None and importlib.util.find_spec("onnxruntime") is None:
        missing.append("onnxruntime")
    if missing:
        return "Python cannot find " + " and ".join(missing)
    return None


def track_speaker(track):
    if track["kind"] in ("microphone", "local"):
        return "me", text("common.me"), "source"
    if track["kind"] != "system":
        return "track-" + track["id"], None, "unnamed"
    return "others", text("common.theOthers"), "source"


def should_auto_write(meeting, previous):
    if (
        previous is None
        or meeting.origin != "own"
        or meeting.lifecycle != "saved"
        or meeting.capture_state not in ("completed", "interrupted")
        or meeting.transcript != "none"
        or not meeting.metadata_editable
        or meeting.directory.parent.name == "sessions"
        and meeting.directory.parent.parent.name == ".cassini"
        and meeting.published_file is None
    ):
        return False
    before = previous.get(meeting.key)
    return (
        before is None
        or before[0] in ("recording", "saving")
        or before[1] not in ("completed", "interrupted")
        or len(before) > 2
        and not before[2]
        and meeting.published_file is not None
    )


def session_presence(track):
    """D52.2 present or unknown time as merged 16 kHz [start, end) ranges from the session origin.

    The job carries only this form: the child derives its track-relative
    ranges from it and the offset (D63 #3 compares a word's midpoint here),
    so the ranges are sent once. None when the track is not placed.
    """
    from .source_ownership import presence, samples

    if track.start_offset_ms is None:
        return None

    def row(value):
        anchors = [list(point) for point in value.clock_anchors]
        return dict(
            offsetMs=value.start_offset_ms,
            sampleRate=value.sample_rate,
            frames=value.frames,
            **({"clockAnchors": anchors} if anchors else {}),
        )

    logical = dict(row(track), gaps=track.gaps)
    if track.spans:
        logical["spans"] = [row(span) for span in track.spans]
    found = presence(logical, track.ownership_windows)
    if found is None:
        return None
    merged = []
    for start, end in sorted(samples((*found.present, *found.unknown))):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def prepare_tracks(meeting, *, diagnostics=None, silent=None):
    """One job per readable track. `silent(track)` marks D56 silent tracks.

    A silent track, including one with no samples at all, keeps its job, its
    speaker row and its place in the D11 layout, flagged `silent`. It is never
    recognised, rejoined or given an overlap lane, so it cannot push a live span
    of its source onto a lane; its speaker ID stays reserved, so no lane takes it.
    A track session.json lists but this reader cannot read keeps its place in
    the lanes, as session.json states it, so a missing file moves no lane; one
    the file's x tracks list keeps its id reserved (library.track_rows and
    source_ownership.speaker_lanes, shared with the gap map).
    """
    # Identity/kind come only from the scanner's validated or recovered model.
    tracks = []
    speakers = {}
    for index, track in enumerate(meeting.tracks):
        identity = track.source_track_id or track_identity(track)
        speaker, name, provenance = track_speaker(dict(id=identity, kind=track.kind))
        if len(speaker) > 256:
            raise ValueError("Capture speaker identifier exceeds metadata limit")
        tracks.append(
            dict(
                id=track.path.name,
                **({"captureTrackId": track.id} if track.id is not None else {}),
                path=str(track.path),
                offsetMs=track.start_offset_ms,
                speaker=speaker,
                kind=track.kind,
                frames=track.frames,
                sampleRate=track.sample_rate,
                # S-12 §5: the child dispatches the decoder by codec; WAV jobs are unchanged.
                **({"codec": "opus"} if track.codec == "opus" else {}),
                **(
                    dict(
                        clockAnchors=[list(point) for point in track.clock_anchors],
                        channels=track.channels,
                        sessionDirectory=str(meeting.directory),
                    )
                    if track.clock_anchors or track.codec == "opus"
                    else {}
                ),
            )
        )
        speakers.setdefault(speaker, dict(id=speaker, name=name, provenance=provenance, colourIndex=index))
        # D56: no samples is fewer than 32 non-zero frames; nothing reads such a file.
        if not track.frames or silent is not None and silent(track):
            tracks[-1]["silent"] = True
        if not track.frames:
            continue
        owned = [span.ownership_windows for span in track.spans if span.ownership_windows is not None]
        if track.ownership_windows is not None:
            tracks[-1]["ownershipWindows"] = [list(window) for window in track.ownership_windows]
        elif owned:
            from dataclasses import replace

            from .source_ownership import union

            # D19: an unplaced span sounds unclipped from its assumed zero; its placed siblings
            # are masked by their own windows (below) and here, after the 16 kHz FIR. The
            # display end is floored to whole ms, so one more ms covers its last samples.
            assumed = [
                (0, replace(span, start_offset_ms=0).display_end_ms + 1)
                for span in track.spans
                if span.start_offset_ms is None
            ]
            tracks[-1]["ownershipWindows"] = [list(window) for window in union(owned[0], assumed)]
        # D52.2: the G10 fallback measures and windows only present or unknown time;
        # D63 #3: the child writes only words whose midpoint lies in it.
        if (present := session_presence(track)) is not None:
            tracks[-1]["sessionPresence"] = present
        if track.spans:
            tracks[-1]["spans"] = [
                dict(
                    file=span.path.relative_to(meeting.directory).as_posix(),
                    frames=span.frames,
                    sampleRate=span.sample_rate,
                    channels=span.channels,
                    startOffsetMs=span.start_offset_ms,
                    **({"clockAnchors": [list(point) for point in span.clock_anchors]} if span.clock_anchors else {}),
                    **({"codec": "opus", "decodedRate": 48000} if span.codec == "opus" else {}),
                    **(
                        {"ownershipWindows": [list(window) for window in span.ownership_windows]}
                        if span.ownership_windows is not None and track.ownership_windows is None
                        else {}
                    ),
                )
                for span in track.spans
            ]
            tracks[-1]["sessionDirectory"] = str(meeting.directory)
    from .source_ownership import speaker_lanes

    # R64 lanes from every track the session or file lists, as the gap map reads them.
    rows, _stored_silent = track_rows(
        meeting.tracks, getattr(meeting, "extensions", None), getattr(meeting, "lane_rows", ())
    )
    left_out = {
        track_identity(scanned) for scanned, job in zip(meeting.tracks, tracks, strict=True) if job.get("silent")
    }
    lanes, separation_diagnostics = speaker_lanes(rows, silent=left_out, strict=True)
    if diagnostics is not None:
        diagnostics.extend(separation_diagnostics)
    lanes = {row.get("id"): lane for row, lane in zip(rows, lanes, strict=True) if lane is not None}
    for scanned, track in zip(meeting.tracks, tracks, strict=True):
        lane = lanes.get(track_identity(scanned))
        if lane is not None and lane != track["speaker"]:
            speakers.setdefault(
                lane,
                dict(
                    speakers[track["speaker"]],
                    id=lane,
                    colourIndex=max(s["colourIndex"] for s in speakers.values()) + 1,
                ),
            )
            track["speaker"] = lane
    for diagnostic in separation_diagnostics:
        LOG.warning("Transcription diagnostic: %s", diagnostic)
    return tracks, speakers


def reserved_speakers(meeting, jobs=()):
    """D64: every speaker id the meeting's listed tracks hold, readable, unreadable or missing.

    Each row's base id and its R64 lane, from the rows the write-down and the
    gap map read (library.track_rows, source_ownership.speaker_lanes), and the
    jobs' own speakers. `jobs` are prepare_tracks' (one per scanned track); their
    D56 silent tracks take no lane, as there.
    """
    from .source_ownership import speaker_lanes, track_speaker_id

    rows, _stored_silent = track_rows(
        meeting.tracks, getattr(meeting, "extensions", None), getattr(meeting, "lane_rows", ())
    )
    left_out = (
        {track_identity(scanned) for scanned, job in zip(meeting.tracks, jobs, strict=True) if job.get("silent")}
        if jobs
        else set()
    )
    lanes, _diagnostics = speaker_lanes(rows, silent=left_out)
    bases = [track_speaker_id(row) for row in rows]
    return {identifier for identifier in bases + lanes if identifier is not None} | {job["speaker"] for job in jobs}


def recorded_silent_tracks(metadata):
    """D56: the ids meeting.json lists in writingDown.silentTracks; absent means none."""
    details = metadata.get("recordingDetails")
    writing = details.get("writingDown") if isinstance(details, dict) else None
    listed = writing.get("silentTracks") if isinstance(writing, dict) else None
    return frozenset(i for i in listed if isinstance(i, str)) if isinstance(listed, list) else frozenset()


def voice_group(identifier):
    """The D11 group of a D24 `<group>~n` voice id, or None for any other id."""
    group, separator, ordinal = identifier.rpartition("~")
    return group if separator and group and ordinal.isdigit() else None


def unassigned_voice(identifier, colour_index, source):
    return dict(id=identifier, name=None, provenance="unnamed", colourIndex=colour_index, voiceSource=dict(source))


def _retained_count(directory):
    """How many words the working snapshot holds, which a new attempt keeps until it catches up.

    D67.2: a failed attempt leaves its partial transcript.json for Try again. The
    retained count is that sidecar's, read through the validated reader, never the
    words a reader shows: a published meeting shows its file's words (D47), which
    a failure leaves as they were, so they would let a shorter retry overwrite the
    longer snapshot. A missing or unreadable sidecar retains nothing.
    """
    try:
        snapshot = transcript_snapshot(directory)
    except (OSError, ValueError):
        return 0
    return len(snapshot["items"]) if snapshot is not None else 0


def update_progress_metadata(
    directory,
    speakers,
    *,
    check_active=None,
    transient=False,
    transcript_body=None,
    voice_sources=None,
    reserved=(),
    **changes,
):
    """Write planned speakers; G12 keeps a voice's name only while its source matches.

    A planned `<group>~n` entry carries `voiceSource`. A previous entry for the
    same id keeps its name only when its stamp is equal; otherwise it becomes
    unassigned, as does any other `<group>~*` entry of a re-diarized group whose
    stamp differs. `voice_sources` adds groups that produced no voice this run.
    D64: a planned track or lane row never inherits an old voice row of its id,
    and a `reserved` id (a listed track's, readable or not) is never one of a
    group's voices, so its row is left as it is.
    """
    with mutation(directory):
        if check_active is not None:
            check_active()
        current = read_metadata(directory)
        existing = {speaker["id"]: speaker for speaker in current["speakers"]}
        merged = dict(existing)
        sources = dict(voice_sources or {})
        for identifier, planned in speakers.items():
            if "voiceSource" in planned and (group := voice_group(identifier)) is not None:
                sources.setdefault(group, planned["voiceSource"])
        reset = {}
        for identifier, planned in speakers.items():
            previous = existing.get(identifier, {})
            if "voiceSource" in planned and previous and previous.get("voiceSource") != planned["voiceSource"]:
                merged[identifier] = unassigned_voice(identifier, planned["colourIndex"], planned["voiceSource"])
                reset[identifier] = previous
                continue
            if "voiceSource" in previous and "voiceSource" not in planned:
                # D64: the id was a voice and is now a track or lane speaker; it keeps nothing of the voice.
                merged[identifier] = dict(planned)
                reset[identifier] = previous
                continue
            merged[identifier] = dict(previous, **planned)
            if previous.get("provenance") in ("user", "recognised", "unnamed"):
                merged[identifier] = previous
        for identifier, previous in existing.items():
            group = voice_group(identifier)
            if (
                identifier in speakers
                or identifier in reserved
                or group not in sources
                or previous.get("voiceSource") == sources[group]
            ):
                continue
            merged[identifier] = unassigned_voice(identifier, previous.get("colourIndex", 0), sources[group])
            reset[identifier] = previous
        changes["speakers"] = list(merged.values())
        history = current.get("speakerMerges")
        if reset and isinstance(history, list):
            kept = [
                entry
                for entry in history
                if not isinstance(entry, dict)
                or not any(isinstance(end, str) and end in reset for end in (entry.get("from"), entry.get("into")))
            ]
            if kept != history:
                changes["speakerMerges"] = kept
        if transcript_body is None and all(current.get(key) == value for key, value in changes.items()):
            return False
        update_metadata(
            directory, before_commit=check_active, transient=transient, transcript_body=transcript_body, **changes
        )
        for identifier, previous in reset.items():
            # No file field: the dropped name is only diagnosed (D51).
            LOG.warning(
                "voiceNameReset id=%s old=%s named=%s",
                identifier,
                previous.get("provenance", "unknown"),
                previous.get("name") is not None,
            )
        return True


class TranscriptionQueue:
    def __init__(
        self,
        changed,
        finished,
        *,
        bundle=None,
        client_factory=WhistleClient,
        root=None,
        models=None,
        diarization_models=None,
        voice_models=None,
        voice_store=None,
        clock=time.monotonic,
        recordings=tuple,
        completed=lambda _model: None,
    ):
        self.changed, self.finished = changed, finished
        self.completed = completed
        # Folders of captures running now. G14 keeps the recorder's reserve
        # free on their volume when the job starts.
        self.recordings = recordings
        self.clock = clock
        self.bundle = Path(bundle) if bundle else bundle_path()
        self.root = library_root(root).absolute()
        self._explicit_root = root is not None
        self.client_factory = client_factory
        self.client = None
        self.models = models
        self.diarization_models = diarization_models
        self.voice_models, self.voice_store = voice_models, voice_store
        self.client_model = None
        self.preparer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-queue-metadata")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-transcription")
        self._stopper = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cassini-transcribe-stop")
        self.pending = set()
        self._resume_pending = set()
        self._voice_reruns = {}
        self.voice_pending = set()
        self._background_voices = {}
        self.lock = threading.RLock()
        self._idle = threading.Condition(self.lock)
        self._cancelled_meetings = set()
        self._stopping_meetings = set()
        self._context = threading.local()
        self._active_directory = None
        self._prepare_futures, self._job_futures, self._voice_futures = {}, {}, {}
        self.closed = False

    def transcribing(self, directory):
        """Whether a job for this meeting is queued or running (it may re-diarize)."""
        with self.lock:
            return Path(directory).absolute() in self.pending

    def enqueue(self, directory, *, resume=False):
        directory = Path(directory).absolute()
        with self.lock:
            if self.closed or directory in self._cancelled_meetings or directory in self._stopping_meetings:
                return False
            if directory in self.pending:
                if resume and directory not in self._job_futures:
                    self._resume_pending.add(directory)
                return False
            self.pending.add(directory)
            if self._active_directory in self._background_voices and self.client:
                self.client.cancel()
            future = self.preparer.submit(self._mark_queued, directory, resume)
            self._prepare_futures[directory] = future
        future.add_done_callback(lambda f: self._prepared(directory, f))
        return True

    def preparing(self):
        """Accepted jobs whose durable queue intent has not finished saving."""
        with self.lock:
            return any(not future.done() for future in self._prepare_futures.values())

    def _owned(self, directory):
        if not self._explicit_root:
            from .library_location import owned_directory

            return owned_directory(directory)
        canonical = directory.resolve()
        from .library_publication import root_for

        if (
            directory.is_symlink()
            or canonical.parent != self.root.resolve()
            and root_for(directory) != self.root.resolve()
            or not meeting_id(canonical)
        ):
            raise ValueError("Transcription may only write inside the configured Library")
        return canonical

    def _mark_queued(self, directory, resume):
        with self._job_scope(directory), transaction(directory):
            return self._mark_queued_locked(directory, resume)

    def _mark_queued_locked(self, directory, resume):
        self._check_open()
        canonical = self._owned(directory)
        directory = canonical
        scanner = LibraryScanner.for_meeting(canonical)
        meeting = next(m for m in scanner.scan(changed_directories={canonical}).meetings if m.directory == canonical)
        metadata = read_metadata(directory)
        if (
            not meeting.metadata_editable
            or not meeting.has_audio
            or meeting.origin == "own"
            and not any(t.frames for t in meeting.tracks)
            and meeting.audio_file is None
        ):
            return None
        from .library_publication import root_for

        if root_for(directory) is not None and meeting.published_file is None:
            if meeting.capture_state in ("completed", "interrupted"):
                update_metadata(
                    directory,
                    transcript=dict(metadata["transcript"], status="queued", reference=REFERENCE),
                    etaMinutes=None,
                    error=None,
                    before_commit=self._check_open,
                    transient=True,
                )
            return None  # Publication/recovery makes this durable intent runnable.
        if (
            meeting.transcript not in ("none", "failed")
            and not meeting.no_speech
            and not (resume and meeting.transcript in ("queued", "running"))
        ):
            return None
        if not (directory / "transcript.json").exists():
            if meeting.published_file is not None and not directory.exists():
                from .library_v2.fsutil import fsync_dir

                directory.mkdir(mode=0o700)
                fsync_dir(directory.parent)
                atomic_json(directory / "meeting.json", metadata, before_commit=self._check_open)
            atomic_json(
                directory / "transcript.json",
                dict(format="cassini.words.v1", language="und", items=[]),
                before_commit=self._check_open,
            )
        update_metadata(
            directory,
            transcript=dict(metadata["transcript"], status="queued", reference=REFERENCE),
            transient=True,
            etaMinutes=None,
            error=None,
            before_commit=self._check_open,
        )
        # A failed body is kept unreferenced (D67.2). Once queued, it is referenced again.
        # How many of its words the job retains is read from that sidecar when it
        # starts (_retained_count), not from these words: a published meeting shows
        # its file's words. An explicit retry follows the same rule as auto-resume.
        meeting = next(m for m in scanner.scan(changed_directories={canonical}).meetings if m.directory == canonical)
        return meeting, metadata

    def _prepared(self, directory, future):
        try:
            prepared = future.result()
        except CancelledError:
            self._completed(directory)
        except Exception as error:
            if not self.closed:
                self._failed(directory, error)
            self._completed(directory)
        else:
            if prepared is None:
                retry = None
                retired = False
                with self.lock:
                    if (
                        directory in self._resume_pending
                        and not self.closed
                        and directory not in self._cancelled_meetings
                        and directory not in self._stopping_meetings
                    ):
                        self._resume_pending.discard(directory)
                        retry = self.preparer.submit(self._mark_queued, directory, True)
                        self._prepare_futures[directory] = retry
                    else:
                        retired = self._retire_locked(directory)
                if retry is not None:
                    retry.add_done_callback(lambda f: self._prepared(directory, f))
                    return
                if retired:
                    self.finished(directory)
            else:
                # The durable queue intent and its Future are both complete
                # before GTK observes this change and considers a restart.
                self.changed(directory)
                retired = False
                with self.lock:
                    self._resume_pending.discard(directory)
                    if (
                        not self.closed
                        and directory not in self._cancelled_meetings
                        and directory not in self._stopping_meetings
                    ):
                        self._prepare_futures.pop(directory, None)
                        job_future = self.executor.submit(self._job, directory, *prepared)
                        self._job_futures[directory] = job_future
                        job_future.add_done_callback(lambda f: self._completed(directory, job_future=f))
                        return
                    retired = self._retire_locked(directory)
                if retired:
                    self.finished(directory)

    def _retire_locked(self, directory, *, job_future=None):
        if directory not in self.pending:
            return False
        if job_future is not None and self._job_futures.get(directory) is not job_future:
            return False
        self.pending.remove(directory)
        self._resume_pending.discard(directory)
        self._prepare_futures.pop(directory, None)
        self._job_futures.pop(directory, None)
        self._idle.notify_all()
        return True

    def _completed(self, directory, *, job_future=None):
        with self.lock:
            retired = self._retire_locked(directory, job_future=job_future)
        if retired:
            self.finished(directory)

    def _failed(self, directory, error):
        if self.closed or directory in self._cancelled_meetings:
            return
        LOG.warning("Meeting transcription failed: %s", error)
        try:
            directory = self._owned(directory)
            with transaction(directory):
                self._check_open()
                # D67.2: a failed transcript shows no words because the file says so.
                # The partial transcript.json stays, unreferenced, for Try again,
                # which references it again when it queues (_mark_queued). No body
                # goes to a published Cassini file either: its default transcript
                # keeps what it held, so no reader sees the partial words.
                update_metadata(
                    directory,
                    transcript=dict(read_metadata(directory)["transcript"], status="failed", reference=None),
                    etaMinutes=None,
                    error=dict(id="transcribe-failed", action="Try again"),
                    before_commit=self._check_open,
                )
            self.changed(directory)
        except CancelledError:
            return
        except (OSError, ValueError):
            LOG.exception("Could not preserve failed transcription state")

    def _job(self, directory, meeting, metadata):
        try:
            with self._job_scope(directory, owner=True):
                self._job_body(directory, meeting, metadata)
        except CancelledError:
            self._completed(directory)

    def _job_body(self, directory, meeting, metadata):
        try:
            if self.closed:
                return
            self._run(meeting, metadata)
        except CancelledError:
            if self.client:
                self.client.close()
                self.client = None
                self.client_model = None
            raise
        except Exception as error:
            if not isinstance(error, CancelledError):
                self._failed(directory, error)
            if self.client:
                self.client.close()
                self.client = None
                self.client_model = None
        finally:
            self._completed(directory)

    def _live_recordings(self):
        try:
            return [str(directory) for directory in self.recordings()][:64]
        except Exception:
            # The 1 GiB floor still applies in the worker.
            LOG.exception("Could not list live recordings for the disk budget")
            return []

    def _run(self, meeting, metadata):
        started = self.clock()
        self._check_open()
        directory = meeting.directory
        # Select on this owner thread when the job starts, never at enqueue.
        from .parakeet_model import MODEL_ID, MODEL_TAG

        selected = self.models.choice_for_job() if self.models else "whistle"
        tag = MODEL_TAG if selected == MODEL_ID else "whistle"
        update_metadata(
            directory,
            transcript=dict(status="running", reference=REFERENCE, model=tag),
            transient=True,
            etaMinutes=None,
            error=None,
            before_commit=self._check_open,
        )
        self.changed(directory)
        if self.client and (self.client_model != selected or selected == MODEL_ID):
            self.client.close()
            self.client = None
        if selected == MODEL_ID:
            self.client = self.models.start_parakeet(self.bundle, register=self._register_client)
            self.client_model = MODEL_ID if self.client else None
        if self.client is None:
            client = self.client_factory(self.bundle, timeout=120)
            self._register_client(client)
            self.client_model = "whistle"
            self.client.start()
        if self.client_model != selected:
            tag = "whistle"
            update_metadata(
                directory,
                transcript=dict(status="running", reference=REFERENCE, model=tag),
                transient=True,
                before_commit=self._check_open,
            )
            self.changed(directory)
        source_diagnostics = []
        if meeting.origin == "received" or meeting.audio_file is not None and not meeting.tracks:
            tracks = [
                dict(
                    id="received",
                    path=str(meeting.audio_file),
                    format="opus",
                    offsetMs=0,
                    speaker="track-original",
                    kind="original",
                    frames=int((meeting.duration_ms or 0) * 16),
                    sampleRate=16000,
                )
            ]
            speakers = {"track-original": dict(id="track-original", name=None, provenance="unnamed", colourIndex=0)}
            # D64: the tracks the file lists keep their ids.
            reserved = reserved_speakers(meeting) | set(speakers)
        else:
            from .silent_tracks import silent

            # D56: only this attempt measures; it writes the list the voice step reads.
            tracks, speakers = prepare_tracks(
                meeting, diagnostics=source_diagnostics, silent=lambda track: silent(track, self._check_open)
            )
            reserved = reserved_speakers(meeting, tracks)
        if not tracks:
            raise ValueError("Meeting has no complete audio frames")
        by_track = {track["id"]: track for track in tracks}
        publisher = TranscriptPublisher(
            directory, existing_count=_retained_count(directory), check_active=self._check_not_trashed
        )
        cache_entries = _read_recognition_cache_entries(directory, tag)
        cache_dirty = False

        def record_cache(entry):
            nonlocal cache_dirty
            if not isinstance(entry, dict) or not isinstance(entry.get("key"), str) or len(entry["key"]) != 64:
                return
            cache_entries[entry["key"]] = {
                "text": entry["text"],
                "words": entry["words"],
                "language": entry["language"],
            }
            cache_dirty = True

        def flush_cache():
            nonlocal cache_dirty
            if not cache_dirty or not cache_entries:
                return
            self._check_not_trashed()
            atomic_json(
                directory / RECOGNITION_CACHE_FILE,
                dict(format=RECOGNITION_CACHE_FORMAT, model=tag, segments=cache_entries),
                before_commit=self._check_not_trashed,
            )
            cache_dirty = False

        diarization = (
            self.diarization_models.request(directory, origin=meeting.origin) if self.diarization_models else None
        )
        live = self._live_recordings()
        if diarization is not None:
            diarization = dict(diarization, recordingDirectories=live)
        from .capture_facts import (
            offsets_assumed_zero,
            recognition_fallback,
            separation_result,
            separation_unavailable,
            silent_tracks,
            writing_details,
        )

        separation = separation_unavailable(self.diarization_models, tracks, meeting.origin)
        voices_options, voice_generation = None, None
        if diarization is not None and self.voice_models and self.voice_store:
            try:
                state = self.voice_store.refresh()
                if not state["readOnly"]:
                    voice_generation = state["generation"]
                    voices_options = self.voice_models.request(directory, origin=meeting.origin)
            except (OSError, ValueError):
                LOG.exception("Saved voices unavailable; keeping ordinary speaker names")
        self.client.keep_for_voices = voices_options is not None
        self.client.diarization = diarization
        self.client.recognition_cache = dict(path=str(directory / RECOGNITION_CACHE_FILE), model=tag)
        turn_groups = {}
        reused_groups = set()
        recognised_summary = None

        def progress(value):
            nonlocal recognised_summary
            if self.closed or directory in self._cancelled_meetings or directory in self._stopping_meetings:
                raise CancelledError()
            if "cacheEntry" in value:
                record_cache(value["cacheEntry"])
            if value["type"] == "planning":
                return
            if value["type"] == "recognised":
                recognised_summary = value["summary"]
            if value["type"] == "diarized":
                from .worker_diarization import valid_group

                group_id = value["groupId"]
                if value["first"]:
                    if value.get("reused") is True:
                        reused_groups.add(group_id)
                    turn_groups[group_id] = dict(
                        id=group_id,
                        durationMs=value["durationMs"],
                        inputSHA256=value["inputSHA256"],
                        turns=[],
                        final=False,
                    )
                group = turn_groups[group_id]
                if len(group["turns"]) + len(value["turns"]) > 100000:
                    raise ValueError("Diarization turn limit exceeded")
                group["turns"].extend(value["turns"])
                group["final"] = value["final"]
                if group["final"] and not valid_group(group):
                    raise ValueError("Invalid diarization result")
                return
            if value["type"] == "progress":
                track = by_track[value["trackId"]]
                for word in value["words"]:
                    if (
                        type(word["startMs"]) is not int
                        or type(word["endMs"]) is not int
                        or not 0 <= word["startMs"] <= word["endMs"]
                        or not word["text"]
                    ):
                        raise ValueError("Invalid timed recognition item")
                publisher.add(value["words"], track["speaker"], value["trackOrder"], value["language"])
                published = publisher.flush()
                if published:
                    flush_cache()
            else:
                published = False
            updated = update_progress_metadata(
                directory, speakers, etaMinutes=value["etaMinutes"], check_active=self._check_open, transient=True
            )
            if published or updated:
                self.changed(directory)

        try:
            try:
                complete = self.client.process_tracks(tracks, progress)
            except Exception as error:
                if self.closed or isinstance(error, CancelledError) or recognised_summary is None:
                    raise
                # Recognition is already complete: a crashing/timed-out native
                # diarizer cannot turn those saved words into a failed job.
                LOG.warning("Diarization worker failed after recognition; keeping D11: %s", error)
                complete = recognised_summary
                self.client.close()
                self.client = self.client_model = None
        except BaseException:
            # Keep the useful partial snapshot on real errors or explicit Stop.
            # App shutdown and Trash retain the last published one without starting a write.
            if not self.closed and directory not in self._cancelled_meetings:
                flush_cache()
                if publisher.flush(force=True):
                    self.changed(directory)
            raise
        self._check_open()
        if diarization is not None and complete.get("diarization"):
            from .diarization import apply_transcript, voice_source
            from .worker_diarization import FORMAT, valid_group

            result = complete["diarization"]
            self.diarization_models.measured(result.get("realtimeFactor"))
            groups = [
                {key: value for key, value in g.items() if key != "final"}
                for identifier, g in turn_groups.items()
                if identifier in result["groupIds"] and g["final"] and valid_group(g)
            ]
            if groups:
                # D64: every listed track's id and lane is taken, readable or not.
                words, voices = apply_transcript(publisher.words, groups, reserved=reserved)
                with transaction(directory):
                    if self.closed:
                        raise CancelledError()
                    self._owned(directory)
                    atomic_json(
                        directory / "speaker-turns.json",
                        dict(format=FORMAT, model=result["model"], params=result["params"], groups=groups),
                        before_commit=self._check_open,
                    )
                    # G12: every re-diarized group, conclusive or not, rebinds its old voice names.
                    sources = {g["id"]: voice_source(g["inputSHA256"], g["turns"]) for g in groups}
                    speakers.update(voices)
                    update_progress_metadata(
                        directory,
                        speakers,
                        check_active=self._check_open,
                        transient=True,
                        voice_sources=sources,
                        reserved=reserved,
                    )
                    if voices:
                        publisher.reassign(words)
            if separation["reason"] != "notApplicable":
                separation = separation_result(result, groups, reused_groups)
            complete.setdefault("diagnostics", []).extend(result.get("diagnostics", []))
        elif diarization is not None and separation["reason"] != "notApplicable":
            separation = dict(status="failed", reason="processingFailed")
        for diagnostic in complete.get("diagnostics", []):
            LOG.warning("Transcription diagnostic: %s", diagnostic)
        self._check_open()
        # A completed transcript is published once, after optional attribution.
        # Until this succeeds the running job remains resumable after restart.
        publisher.flush(force=True, complete=True)
        if voices_options and self.client is not None:
            try:
                with transaction(directory):
                    history = read_metadata(directory).get("speakerMerges", [])
                prints = self.client.voiceprints(
                    tracks,
                    # G14: recordings live now, not at job start: one may have begun while writing down.
                    dict(
                        voices_options,
                        directory=str(directory),
                        speakerMerges=history,
                        recordingDirectories=self._live_recordings(),
                        reservedSpeakers=sorted(reserved),
                    ),
                    self._check_open,
                )
                self._check_open()

                def apply_names(names):
                    with mutation(directory):
                        self._check_open()
                        self._owned(directory)
                        current = read_metadata(directory)
                        if current.get("speakerMerges", []) != history:
                            raise ValueError("Speaker merges changed during voice extraction")
                        entries = {s["id"]: dict(s) for s in current["speakers"]}
                        for identifier, name in names.items():
                            previous = entries.get(identifier, {})
                            if (
                                previous.get("provenance") != "user"
                                and not previous.get("nameCleared")
                                and identifier in speakers
                            ):
                                speakers[identifier] = dict(speakers[identifier], name=name, provenance="recognised")
                                entries[identifier] = speakers[identifier]
                        if list(entries.values()) != current["speakers"]:
                            update_metadata(directory, speakers=list(entries.values()), before_commit=self._check_open)

                def voice_active():
                    self._check_open()
                    if read_metadata(directory).get("speakerMerges", []) != history:
                        raise ValueError("Speaker merges changed during voice extraction")

                def auto_eligible():
                    # commit_job evaluates this under the same metadata lock
                    # held through naming, so a concurrent Clear cannot race it.
                    protected = {
                        s["id"]
                        for s in read_metadata(directory)["speakers"]
                        if s.get("provenance") == "user" or s.get("nameCleared")
                    }
                    return prints.keys() - protected

                self.voice_store.commit_job(
                    directory,
                    prints,
                    voice_generation,
                    check_active=voice_active,
                    apply_names=apply_names,
                    auto_eligible=auto_eligible,
                    voice_sources={s: e["voiceSource"] for s, e in speakers.items() if "voiceSource" in e},
                )
            except CancelledError:
                raise
            except Exception:
                LOG.exception("Saved voice extraction failed; keeping the completed transcript")
                self.client.close()
                self.client = self.client_model = None
        facts = dict(
            wallDurationMs=max(0, min(9007199254740991, int((self.clock() - started) * 1000))),
            resumed=(metadata.get("transcript") or {}).get("status") in ("queued", "running"),
            speakerSeparation=separation,
        )
        segments = complete.get("segments")
        if type(segments) is int and 0 <= segments <= 9007199254740991:
            facts["speechSegments"] = segments
        if source_diagnostics:
            facts["sourceSpansSeparated"] = True
        if (fallback := recognition_fallback(complete.get("fallback"))) is not None:
            facts["recognitionFallback"] = fallback
        if offsets_assumed_zero(tracks):
            facts["offsetsAssumedZero"] = True
        if left_out := silent_tracks(tracks):
            facts["silentTracks"] = left_out
        with mutation(directory):
            self._check_open()
            # End facts or other metadata may have arrived while this job ran.
            details = writing_details(read_metadata(directory).get("recordingDetails", {}), facts)
            update_progress_metadata(
                directory,
                speakers,
                transcript=dict(status="done", reference=REFERENCE, model=tag),
                transcript_body=transcript_snapshot(directory),
                recordingDetails=details,
                etaMinutes=None,
                error=None,
                check_active=self._check_open,
            )
        with contextlib.suppress(OSError):
            (directory / RECOGNITION_CACHE_FILE).unlink()
        self.changed(directory)
        self.completed(tag)
        if self.client_model == MODEL_ID:
            self.client.close()
            self.client = None
            self.client_model = None

    def recompute_voices(self, directory, affected, failed):
        token = Path(directory).absolute()
        with self.lock:
            if self.closed or token in self._cancelled_meetings or self.voice_models is None:
                return False
            if not self.voice_models.snapshot().get("ready") or not self.diarization_models.snapshot().get("ready"):
                return False
            if token in self.voice_pending:
                previous, _callback = self._voice_reruns.get(token, (set(), failed))
                self._voice_reruns[token] = previous | set(affected), failed
                return False
            self.voice_pending.add(token)
            self._submit_voice(token, set(affected), failed)
        return True

    def backfill_voices(self, directory, finished):
        """Admit one historical meeting only while the foreground queue is idle.

        Background completion has no foreground app hold to release. Its
        worker is cancelled if writing down arrives, and Trash uses the same
        per-meeting exclusion as every other voice job.
        """
        token = Path(directory).absolute()
        with self.lock:
            if (
                self.closed
                or self.pending
                or self.voice_pending
                or token in self._cancelled_meetings
                or self.voice_models is None
                or self.diarization_models is None
                or not self.voice_models.snapshot().get("ready")
                or not self.diarization_models.snapshot().get("ready")
            ):
                return False
            self._background_voices[token] = finished
            self.voice_pending.add(token)
            self._submit_voice(token, None, lambda error: LOG.warning("Historical voice preparation failed: %s", error))
        return True

    def _submit_voice(self, token, affected, failed):
        future = self.executor.submit(self._voice_job, token, affected, failed)
        self._voice_futures[token] = future
        future.add_done_callback(lambda f: self._voice_completed(token) if f.cancelled() else None)

    def _voice_completed(self, token):
        with self.lock:
            if token not in self.voice_pending:
                return
            self.voice_pending.remove(token)
            self._voice_futures.pop(token, None)
            self._voice_reruns.pop(token, None)
            background = self._background_voices.pop(token, None)
            preempted = background is not None and bool(self.pending)
            self._idle.notify_all()
        if background is None:
            self.finished(token)
        else:
            background(token, preempted)

    def _voice_job(self, token, affected, failed):
        try:
            with self._job_scope(token, owner=True):
                self._voice_job_body(token, affected, failed)
        except CancelledError:
            self._voice_completed(token)

    def _voice_job_body(self, token, affected, failed):
        directory = token
        try:
            self._check_open()
            directory = self._owned(directory)
            state = self.voice_store.refresh()
            if state["readOnly"]:
                return
            meeting = next(
                m
                for m in LibraryScanner.for_meeting(directory).scan(changed_directories={directory}).meetings
                if m.directory == directory
            )
            if not meeting.metadata_editable:
                return
            background = affected is None
            if background and (meeting.lifecycle != "saved" or meeting.transcript != "done" or not meeting.words):
                return
            options = self.voice_models.request(directory, origin=meeting.origin)
            if not options:
                return
            options = dict(options, recordingDirectories=self._live_recordings())
            if background:
                diarization = self.diarization_models.request(directory, origin=meeting.origin)
                if not diarization:
                    return
                options = dict(options, prepareMissing=True, diarization=diarization)
            # D56/D47: the voice step leaves out exactly the tracks the file
            # lists as silent, whoever wrote it; it never measures again.
            listed = recorded_silent_tracks(meeting.metadata or {})
            tracks, _speakers = prepare_tracks(meeting, silent=lambda track: track.id in listed)
            # D64: the write-down's reservations, so no voice takes a listed track's id.
            reserved = reserved_speakers(meeting, tracks)
            if meeting.origin == "received":
                tracks = [
                    dict(
                        id="received",
                        path=str(meeting.audio_file),
                        format="opus",
                        offsetMs=0,
                        speaker="track-original",
                        kind="original",
                        frames=int((meeting.duration_ms or 0) * 16),
                        sampleRate=16000,
                    )
                ]
            if self.client is not None:
                self.client.close()
                self.client = self.client_model = None
            from .voice_models import VoiceJobClient

            with transaction(directory):
                history = read_metadata(directory).get("speakerMerges", [])
            self._register_client(
                VoiceJobClient(self.bundle, options["modelPath"], options["runtimePath"], timeout=120)
            )
            self.client.start()
            prints = self.client.voiceprints(
                tracks,
                dict(options, directory=str(directory), speakerMerges=history, reservedSpeakers=sorted(reserved)),
                self._check_open,
            )
            self._check_open()

            def current():
                self._check_open()
                if read_metadata(directory).get("speakerMerges", []) != history:
                    raise CancelledError()

            if background:
                # No re-matching or transcript/name writes for historical meetings.
                # Only missing prints are added; existing confirmed data/rejections stay.
                # An item stamped for another voice of its id counts as missing (G12).
                cache, _prepared = self.voice_store.preparation(directory)
                affected = prints.keys() - cache["speakers"].keys()
            self.voice_store.recompute(directory, prints, affected, state["generation"], check_active=current)
            self.changed(directory)
        except Exception as error:
            if not self.closed and not isinstance(error, CancelledError):
                LOG.exception("Voice recomputation failed; keeping previous private prints")
                failed(error)
        finally:
            if self.client is not None:
                self.client.close()
                self.client = self.client_model = None
            with self.lock:
                again = self._voice_reruns.pop(token, None)
                if self.closed or token in self._cancelled_meetings:
                    again = None
                if again is not None:
                    self._submit_voice(token, *again)
                else:
                    self._voice_completed(token)

    @contextlib.contextmanager
    def _job_scope(self, directory, *, owner=False):
        self._context.directory = directory
        self._context.background = owner and directory in self._background_voices
        try:
            with self.lock:
                self._check_open()
                if owner:
                    self._active_directory = directory
            yield
        finally:
            with self.lock:
                if owner and self._active_directory == directory:
                    self._active_directory = None
                self._context.directory = None
                self._context.background = False
                self._idle.notify_all()

    @contextlib.contextmanager
    def cancel_meeting(self, directory):
        """Worker-only exclusion: cancel this meeting, wait, keep it blocked during Trash."""
        directory = Path(directory).absolute()
        with self.lock:
            self._cancelled_meetings.add(directory)
            self._resume_pending.discard(directory)
            futures = [
                mapping.get(directory) for mapping in (self._prepare_futures, self._job_futures, self._voice_futures)
            ]
            # Cancel while holding the owner lock: a persistent client must not
            # advance to another meeting between this identity check and cancel.
            if self._active_directory == directory and self.client and hasattr(self.client, "cancel"):
                self.client.cancel()
        for future in futures:
            if future is not None:
                future.cancel()
        try:
            with self._idle:
                self._idle.wait_for(
                    lambda: (
                        directory not in self.pending
                        and directory not in self.voice_pending
                        and self._active_directory != directory
                    )
                )
            yield
        finally:
            with self.lock:
                self._cancelled_meetings.discard(directory)

    def stop(self, directory, done=lambda: None):
        """Cancel any queued or running transcription for directory and reset its status to none."""
        directory = Path(directory).absolute()
        with self.lock:
            if self.closed or directory in self._cancelled_meetings:
                return False
            self._stopping_meetings.add(directory)
            self._resume_pending.discard(directory)
            futures = [mapping.get(directory) for mapping in (self._prepare_futures, self._job_futures)]
            if self._active_directory == directory and self.client and hasattr(self.client, "cancel"):
                self.client.cancel()
        for future in futures:
            if future is not None:
                future.cancel()
        try:
            self._stopper.submit(self._finish_stop, directory, done)
        except RuntimeError:
            with self.lock:
                self._stopping_meetings.discard(directory)
            return False
        return True

    def _abort_if_trashed(self, directory):
        if self.closed or directory in self._cancelled_meetings:
            raise CancelledError()

    def _finish_stop(self, directory, done):
        target = None
        changed = False
        try:
            with self._idle:
                self._idle.wait_for(
                    lambda: self.closed or (directory not in self.pending and self._active_directory != directory)
                )
            if self.closed or directory in self._cancelled_meetings:
                return
            target = self._owned(directory)
            with transaction(target):
                self._abort_if_trashed(directory)
                current = read_metadata(target)
                tx = dict(current.get("transcript") or {})
                if tx.get("status") in ("queued", "running"):
                    prev_ref = tx.get("reference")
                    if _retained_count(target) > 0:
                        ref = REFERENCE
                    elif isinstance(prev_ref, dict) and prev_ref.get("kind") == "embedded":
                        ref = prev_ref
                    else:
                        ref = None
                    tx["status"] = "none"
                    tx["reference"] = ref
                    update_metadata(
                        target,
                        transcript=tx,
                        etaMinutes=None,
                        error=None,
                        before_commit=lambda: self._abort_if_trashed(directory),
                    )
                    changed = True
        except CancelledError:
            return
        except (OSError, ValueError):
            LOG.exception("Could not save stopped transcription state")
        finally:
            with self.lock:
                self._stopping_meetings.discard(directory)
                self._idle.notify_all()
            if changed and target is not None:
                self.changed(target)
            done()

    def _check_not_trashed(self):
        if self.closed or getattr(self._context, "directory", None) in self._cancelled_meetings:
            raise CancelledError()

    def _check_open(self):
        if (
            self.closed
            or getattr(self._context, "directory", None) in self._cancelled_meetings
            or getattr(self._context, "directory", None) in self._stopping_meetings
            or getattr(self._context, "background", False)
            and self.pending
        ):
            raise CancelledError()

    def _register_client(self, client):
        with self.lock:
            self._check_open()
            self.client = client

    def close(self):
        """Cancel promptly; callbacks/IO teardown finish off the GTK thread."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self._resume_pending.clear()
            client = self.client
            self._idle.notify_all()
        if client and hasattr(client, "cancel"):
            client.cancel()
        self.preparer.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self._stopper.shutdown(wait=False, cancel_futures=True)

        # This cleanup is queued on the owner thread so selector/pipe mutation
        # cannot race process_tracks. It also reaps an idle persistent child.
        def reap():
            if self.client:
                self.client.close()
                self.client = None

        threading.Thread(
            target=self._reap_after_jobs, args=(reap,), name="cassini-transcription-close", daemon=True
        ).start()

    def _reap_after_jobs(self, reap):
        self.preparer.shutdown(wait=True, cancel_futures=True)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self._stopper.shutdown(wait=True, cancel_futures=True)
        reap()
