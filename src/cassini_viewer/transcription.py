# SPDX-License-Identifier: GPL-3.0-or-later
"""One FIFO per app and one persistent inference child; no engine import in GTK."""

import importlib.util
import logging
import os
import platform
import sys
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

from .library import LibraryScanner, library_root, meeting_id
from .meeting_store import LOCK, atomic_json, read_metadata, update_metadata
from .strings import text
from .transcript_publish import TranscriptPublisher
from .whistle_client import WhistleClient

LOG = logging.getLogger(__name__)
REFERENCE = dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")


def bundle_path():
    return Path(os.environ.get("CASSINI_WHISTLE_BUNDLE", Path(__file__).resolve().parent.parent / "whistle"))


def unavailable_reason(bundle=None):
    """Why this computer cannot write meetings down, or None when it can.

    Cheap and import-free: GTK must never import NumPy, ONNX Runtime or the
    engine, so dependencies are only located, not loaded. The worker still
    verifies every bundle hash before it loads anything.
    """
    if sys.platform != "linux" or platform.machine() != "x86_64":
        return f"the offline engine runs only on Linux x86_64, not {sys.platform} {platform.machine()}"
    libc = platform.libc_ver()[0]
    if libc != "glibc":
        return f"the offline engine needs glibc, not {libc or 'this C library'}"
    bundle = Path(bundle) if bundle else bundle_path()
    if not (bundle / "pins.json").is_file():
        return f"the offline engine is not installed (no {bundle / 'pins.json'})"
    missing = [name for name in ("numpy", "onnxruntime") if importlib.util.find_spec(name) is None]
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
    ):
        return False
    before = previous.get(meeting.key)
    return before is None or before[0] in ("recording", "saving") or before[1] not in ("completed", "interrupted")


def prepare_tracks(meeting):
    # Identity/kind come only from the scanner's validated or recovered model.
    from uuid import NAMESPACE_URL, uuid5

    tracks = []
    speakers = {}
    for index, track in enumerate(meeting.tracks):
        if not track.frames:
            continue
        identity = track.id or "recovered-" + str(uuid5(NAMESPACE_URL, track.path.name))
        if len(identity) > 194:
            identity = str(uuid5(NAMESPACE_URL, identity))
        speaker, name, provenance = track_speaker(dict(id=identity, kind=track.kind))
        if len(speaker) > 200:
            raise ValueError("Capture speaker identifier exceeds metadata limit")
        tracks.append(dict(id=track.path.name, path=str(track.path), offsetMs=track.start_offset_ms, speaker=speaker))
        speakers[speaker] = dict(id=speaker, name=name, provenance=provenance, colourIndex=index)
    return tracks, speakers


def update_progress_metadata(directory, speakers, **changes):
    with LOCK:
        current = read_metadata(directory)
        existing = {speaker["id"]: speaker for speaker in current["speakers"]}
        merged = dict(existing)
        for identifier, planned in speakers.items():
            previous = existing.get(identifier, {})
            merged[identifier] = dict(previous, **planned)
            if previous.get("provenance") in ("user", "recognised"):
                merged[identifier] = previous
        changes["speakers"] = list(merged.values())
        if all(current.get(key) == value for key, value in changes.items()):
            return False
        update_metadata(directory, **changes)
        return True


class TranscriptionQueue:
    def __init__(self, changed, finished, *, bundle=None, client_factory=WhistleClient, root=None):
        self.changed, self.finished = changed, finished
        self.bundle = Path(bundle) if bundle else bundle_path()
        self.root = library_root(root).absolute()
        self.client_factory = client_factory
        self.client = None
        self.preparer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-queue-metadata")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-transcription")
        self.pending = set()
        self.lock = threading.Lock()
        self.closed = False

    def enqueue(self, directory, *, resume=False):
        directory = Path(directory).absolute()
        with self.lock:
            if self.closed or directory in self.pending:
                return False
            self.pending.add(directory)
            future = self.preparer.submit(self._mark_queued, directory, resume)
        future.add_done_callback(lambda f: self._prepared(directory, f))
        return True

    def _owned(self, directory):
        canonical = directory.resolve()
        if directory.is_symlink() or canonical.parent != self.root.resolve() or not meeting_id(canonical):
            raise ValueError("Transcription may only write inside the configured Library")
        return canonical

    def _mark_queued(self, directory, resume):
        canonical = self._owned(directory)
        meeting = next(
            m
            for m in LibraryScanner(canonical.parent).scan(changed_directories={canonical}).meetings
            if m.directory == canonical
        )
        metadata = read_metadata(directory)
        if (
            not meeting.metadata_editable
            or not meeting.has_audio
            or meeting.origin == "own"
            and not any(t.frames for t in meeting.tracks)
        ):
            return None
        if meeting.transcript not in ("none", "failed") and not (
            resume and meeting.transcript in ("queued", "running")
        ):
            return None
        if not (directory / "transcript.json").exists():
            atomic_json(directory / "transcript.json", dict(format="cassini.words.v1", language="und", items=[]))
        update_metadata(directory, transcript=dict(status="queued", reference=REFERENCE), etaMinutes=None, error=None)
        self.changed(directory)
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
                with self.lock:
                    if not self.closed:
                        future = self.executor.submit(self._job, directory, *prepared)
                        future.add_done_callback(lambda _f: self._completed(directory))
                        return
                self._completed(directory)

    def _completed(self, directory):
        with self.lock:
            if directory not in self.pending:
                return
            self.pending.remove(directory)
        self.finished(directory)

    def _failed(self, directory, error):
        if self.closed:
            return
        LOG.warning("Meeting transcription failed: %s", error)
        try:
            self._owned(directory)
            update_metadata(
                directory,
                transcript=dict(status="failed", reference=REFERENCE),
                etaMinutes=None,
                error=dict(id="transcribe-failed", action="Try again"),
            )
            self.changed(directory)
        except (OSError, ValueError):
            LOG.exception("Could not preserve failed transcription state")

    def _job(self, directory, meeting, metadata):
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
        directory = meeting.directory
        update_metadata(directory, transcript=dict(status="running", reference=REFERENCE), etaMinutes=None, error=None)
        self.changed(directory)
        if meeting.origin == "received":
            tracks = [
                dict(id="received", path=str(meeting.audio_file), format="opus", offsetMs=0, speaker="track-original")
            ]
            speakers = {"track-original": dict(id="track-original", name=None, provenance="unnamed", colourIndex=0)}
        else:
            tracks, speakers = prepare_tracks(meeting)
        if not tracks:
            raise ValueError("Meeting has no complete audio frames")
        by_track = {track["id"]: track for track in tracks}
        publisher = TranscriptPublisher(directory, existing_count=len(meeting.words))

        def progress(value):
            if self.closed:
                raise CancelledError()
            if value["type"] == "planning":
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
            updated = update_progress_metadata(directory, speakers, etaMinutes=value["etaMinutes"])
            if published or updated:
                self.changed(directory)

        if self.client is None:
            client = self.client_factory(self.bundle, timeout=120)
            with self.lock:
                if self.closed:
                    raise CancelledError()
                self.client = client
            self.client.start()
        completed = False
        try:
            complete = self.client.process_tracks(tracks, progress)
            completed = True
        finally:
            if publisher.flush(force=True, complete=completed):
                self.changed(directory)
        if self.closed:
            raise CancelledError()
        for diagnostic in complete.get("diagnostics", []):
            LOG.warning("Transcription diagnostic: %s", diagnostic)
        update_progress_metadata(
            directory, speakers, transcript=dict(status="done", reference=REFERENCE), etaMinutes=None, error=None
        )
        self.changed(directory)

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
