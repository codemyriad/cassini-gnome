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

from .library import LibraryScanner, library_root, meeting_id
from .library_edits import transaction
from .meeting_store import atomic_json, read_metadata, transcript_snapshot, update_metadata
from .strings import text
from .transcript_publish import TranscriptPublisher
from .whistle_client import WhistleClient
from .whistle_worker import onnxruntime_dir

LOG = logging.getLogger(__name__)
REFERENCE = dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")


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


def prepare_tracks(meeting):
    # Identity/kind come only from the scanner's validated or recovered model.
    from uuid import NAMESPACE_URL, uuid5

    tracks = []
    speakers = {}
    for index, track in enumerate(meeting.tracks):
        if not track.frames:
            continue
        identity = track.source_track_id or track.id or "recovered-" + str(uuid5(NAMESPACE_URL, track.path.name))
        speaker, name, provenance = track_speaker(dict(id=identity, kind=track.kind))
        if len(speaker) > 256:
            raise ValueError("Capture speaker identifier exceeds metadata limit")
        tracks.append(
            dict(
                id=track.path.name,
                path=str(track.path),
                offsetMs=track.start_offset_ms,
                speaker=speaker,
                kind=track.kind,
                frames=track.frames,
                sampleRate=track.sample_rate,
                # S-12 §5: the child dispatches the decoder by codec; WAV jobs are unchanged.
                **({"codec": "opus"} if track.codec == "opus" else {}),
            )
        )
        speakers.setdefault(speaker, dict(id=speaker, name=name, provenance=provenance, colourIndex=index))
    return tracks, speakers


def update_progress_metadata(
    directory, speakers, *, check_active=None, transient=False, transcript_body=None, **changes
):
    with transaction(directory):
        if check_active is not None:
            check_active()
        current = read_metadata(directory)
        existing = {speaker["id"]: speaker for speaker in current["speakers"]}
        merged = dict(existing)
        for identifier, planned in speakers.items():
            previous = existing.get(identifier, {})
            merged[identifier] = dict(previous, **planned)
            if previous.get("provenance") in ("user", "recognised", "unnamed"):
                merged[identifier] = previous
        changes["speakers"] = list(merged.values())
        if transcript_body is None and all(current.get(key) == value for key, value in changes.items()):
            return False
        update_metadata(
            directory, before_commit=check_active, transient=transient, transcript_body=transcript_body, **changes
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
    ):
        self.changed, self.finished = changed, finished
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
        self.pending = set()
        self._voice_reruns = {}
        self.voice_pending = set()
        self._background_voices = {}
        self.lock = threading.RLock()
        self._idle = threading.Condition(self.lock)
        self._cancelled_meetings = set()
        self._context = threading.local()
        self._active_directory = None
        self._prepare_futures, self._job_futures, self._voice_futures = {}, {}, {}
        self.closed = False

    def enqueue(self, directory, *, resume=False):
        directory = Path(directory).absolute()
        with self.lock:
            if self.closed or directory in self.pending or directory in self._cancelled_meetings:
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
        # Failed bodies are intentionally hidden by the Library. Once queued,
        # read the preserved sidecar through the normal validated reader so the
        # publisher knows how many words it must retain until recomputation
        # catches up. An explicit retry follows the same rule as auto-resume.
        meeting = next(m for m in scanner.scan(changed_directories={canonical}).meetings if m.directory == canonical)
        return meeting, metadata

    def _prepared(self, directory, future):
        try:
            prepared = future.result()
        except Exception as error:
            if not self.closed:
                self._failed(directory, error)
            self._completed(directory)
        else:
            if prepared is None:
                self._completed(directory)
            else:
                # The durable queue intent and its Future are both complete
                # before GTK observes this change and considers a restart.
                self.changed(directory)
                with self.lock:
                    if not self.closed and directory not in self._cancelled_meetings:
                        future = self.executor.submit(self._job, directory, *prepared)
                        self._job_futures[directory] = future
                        future.add_done_callback(lambda _f: self._completed(directory))
                        return
                self._completed(directory)

    def _completed(self, directory):
        with self.lock:
            if directory not in self.pending:
                return
            self.pending.remove(directory)
            self._prepare_futures.pop(directory, None)
            self._job_futures.pop(directory, None)
            self._idle.notify_all()
        self.finished(directory)

    def _failed(self, directory, error):
        if self.closed or directory in self._cancelled_meetings:
            return
        LOG.warning("Meeting transcription failed: %s", error)
        try:
            directory = self._owned(directory)
            with transaction(directory):
                self._check_open()
                update_metadata(
                    directory,
                    transcript=dict(read_metadata(directory)["transcript"], status="failed", reference=REFERENCE),
                    transcript_body=transcript_snapshot(directory),
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
        except Exception as error:
            if not isinstance(error, CancelledError):
                self._failed(directory, error)
            if self.client:
                self.client.close()
                self.client = None
        finally:
            self._completed(directory)

    def _run(self, meeting, metadata):
        started = self.clock()
        self._check_open()
        directory = meeting.directory
        # Select on this owner thread when the job starts, never at enqueue.
        from .parakeet_model import MODEL_ID, MODEL_TAG

        selected = self.models.snapshot()["selected"] if self.models else "whistle"
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
            speakers = {"track-original": dict(id="track-original", name=None, provenance="unnamed", colourIndex=0)}
        else:
            tracks, speakers = prepare_tracks(meeting)
        if not tracks:
            raise ValueError("Meeting has no complete audio frames")
        by_track = {track["id"]: track for track in tracks}
        publisher = TranscriptPublisher(directory, existing_count=len(meeting.words), check_active=self._check_open)
        diarization = (
            self.diarization_models.request(directory, origin=meeting.origin) if self.diarization_models else None
        )
        from .capture_facts import separation_result, separation_unavailable, writing_details

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
        turn_groups = {}
        reused_groups = set()
        recognised_summary = None

        def progress(value):
            nonlocal recognised_summary
            if self.closed:
                raise CancelledError()
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
            # Keep the useful partial snapshot on real errors. Cancellation
            # retains the last published one and never starts a final write.
            if not self.closed and publisher.flush(force=True):
                self.changed(directory)
            raise
        if self.closed:
            raise CancelledError()
        if diarization is not None and complete.get("diarization"):
            from .diarization import apply_transcript
            from .worker_diarization import FORMAT, valid_group

            result = complete["diarization"]
            self.diarization_models.measured(result.get("realtimeFactor"))
            groups = [
                {key: value for key, value in g.items() if key != "final"}
                for identifier, g in turn_groups.items()
                if identifier in result["groupIds"] and g["final"] and valid_group(g)
            ]
            if groups:
                words, voices = apply_transcript(publisher.words, groups)
                with transaction(directory):
                    if self.closed:
                        raise CancelledError()
                    self._owned(directory)
                    atomic_json(
                        directory / "speaker-turns.json",
                        dict(format=FORMAT, model=result["model"], params=result["params"], groups=groups),
                        before_commit=self._check_open,
                    )
                    if voices:
                        speakers.update(voices)
                        update_progress_metadata(directory, speakers, check_active=self._check_open, transient=True)
                        publisher.reassign(words)
            if separation["reason"] != "notApplicable":
                separation = separation_result(result, groups, reused_groups)
            complete.setdefault("diagnostics", []).extend(result.get("diagnostics", []))
        elif diarization is not None and separation["reason"] != "notApplicable":
            separation = dict(status="failed", reason="processingFailed")
        for diagnostic in complete.get("diagnostics", []):
            LOG.warning("Transcription diagnostic: %s", diagnostic)
        if self.closed:
            raise CancelledError()
        # A completed transcript is published once, after optional attribution.
        # Until this succeeds the running job remains resumable after restart.
        publisher.flush(force=True, complete=True)
        if voices_options and self.client is not None:
            try:
                with transaction(directory):
                    history = read_metadata(directory).get("speakerMerges", [])
                prints = self.client.voiceprints(
                    tracks, dict(voices_options, directory=str(directory), speakerMerges=history), self._check_open
                )
                self._check_open()

                def apply_names(names):
                    with transaction(directory):
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
        with transaction(directory):
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
        self.changed(directory)
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
            if background:
                diarization = self.diarization_models.request(directory, origin=meeting.origin)
                if not diarization:
                    return
                options = dict(options, prepareMissing=True, diarization=diarization)
            tracks, _speakers = prepare_tracks(meeting)
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
                tracks, dict(options, directory=str(directory), speakerMerges=history), self._check_open
            )
            self._check_open()

            def current():
                self._check_open()
                if read_metadata(directory).get("speakerMerges", []) != history:
                    raise CancelledError()

            if background:
                # No re-matching or transcript/name writes for historical meetings.
                # Only missing prints are added; existing confirmed data/rejections stay.
                cache = self.voice_store.meeting(directory)
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

    def _check_open(self):
        if (
            self.closed
            or getattr(self._context, "directory", None) in self._cancelled_meetings
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
            client = self.client
        if client and hasattr(client, "cancel"):
            client.cancel()
        self.preparer.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)

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
        reap()
