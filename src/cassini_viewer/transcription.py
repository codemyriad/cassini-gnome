# SPDX-License-Identifier: GPL-3.0-or-later
"""One FIFO per app and one persistent inference child; no engine import in GTK."""

import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .library import LibraryScanner
from .meeting_store import atomic_json, read_metadata, update_metadata
from .strings import text
from .whistle_client import WhistleClient

LOG = logging.getLogger(__name__)
REFERENCE = dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")


def bundle_path():
    return Path(os.environ.get("CASSINI_WHISTLE_BUNDLE", Path(__file__).resolve().parent.parent / "whistle"))


def track_speaker(track):
    if track["kind"] in ("microphone", "local"):
        return "me", text("common.me"), "source"
    if track["kind"] == "remote":
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
    # Capture manifests are never modified. The scanner has validated/salvaged
    # actual PCM headers, including unknown-offset diagnostics.
    import json

    try:
        manifest = json.loads((meeting.directory / "session.json").read_text())
        source = {t["file"]: t for t in manifest.get("tracks", []) if isinstance(t, dict) and "file" in t}
    except (OSError, ValueError, TypeError):
        source = {}
    tracks = []
    speakers = {}
    for index, track in enumerate(meeting.tracks):
        if not track.frames:
            continue
        raw = source.get(track.path.name, {})
        # Salvaged named native tracks retain their physical source kind.
        fallback = "microphone" if track.path.name == "track-microphone.wav" else "system"
        raw = dict(id=raw.get("id", str(index)), kind=raw.get("kind", fallback))
        speaker, name, provenance = track_speaker(raw)
        if len(speaker) > 200:
            raise ValueError("Capture speaker identifier exceeds metadata limit")
        tracks.append(dict(id=raw["id"], path=str(track.path), offsetMs=track.start_offset_ms, speaker=speaker))
        speakers[speaker] = dict(id=speaker, name=name, provenance=provenance, colourIndex=index)
    return tracks, speakers


def decode_received(path, destination):
    # Opus decodes natively to 48 kHz PCM. Conversion here preserves the native
    # rate/channels; only the shared FIR ever changes 48 kHz to 16 kHz.
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    pipeline = Gst.parse_launch(
        "uridecodebin name=source ! audioconvert ! audio/x-raw,format=S16LE,rate=48000 ! wavenc ! filesink name=output"
    )
    pipeline.get_by_name("source").set_property("uri", Path(path).as_uri())
    pipeline.get_by_name("output").set_property("location", str(destination))
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not decode received audio")
        message = pipeline.get_bus().timed_pop_filtered(120 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is None:
            raise RuntimeError("Received audio decode timed out")
        if message.type == Gst.MessageType.ERROR:
            raise RuntimeError(message.parse_error()[0].message)
    finally:
        pipeline.set_state(Gst.State.NULL)


class TranscriptionQueue:
    def __init__(self, changed, finished, *, bundle=None, client_factory=WhistleClient):
        self.changed, self.finished = changed, finished
        self.bundle = Path(bundle) if bundle else bundle_path()
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

    def _mark_queued(self, directory, resume):
        canonical = directory.resolve()
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
        atomic_json(directory / "transcript.json", dict(format="cassini.words.v1", language="und", items=[]))
        update_metadata(directory, transcript=dict(status="queued", reference=REFERENCE), etaMinutes=None, error=None)
        self.changed(directory)
        return meeting, metadata

    def _prepared(self, directory, future):
        try:
            prepared = future.result()
        except Exception as error:
            self._failed(directory, error)
            self._completed(directory)
        else:
            if prepared is None:
                self._completed(directory)
            else:
                self.executor.submit(self._job, directory, *prepared)

    def _completed(self, directory):
        with self.lock:
            self.pending.discard(directory)
        self.finished(directory)

    def _failed(self, directory, error):
        LOG.warning("Meeting transcription failed: %s", error)
        try:
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
            self._run(meeting, metadata)
        except Exception as error:
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
        with tempfile.TemporaryDirectory(prefix="cassini-transcribe-") as temporary:
            if meeting.origin == "received":
                decoded = Path(temporary) / "received.wav"
                decode_received(meeting.audio_file, decoded)
                tracks = [dict(id="received", path=str(decoded), offsetMs=0, speaker="track-original")]
                speakers = {"track-original": dict(id="track-original", name=None, provenance="unnamed", colourIndex=0)}
            else:
                tracks, speakers = prepare_tracks(meeting)
            if not tracks:
                raise ValueError("Meeting has no complete audio frames")
            existing = {s["id"]: s for s in metadata.get("speakers", [])}
            for speaker in speakers:
                if speaker in existing and existing[speaker]["provenance"] in ("user", "recognised"):
                    speakers[speaker] = existing[speaker]
            by_track = {track["id"]: track for track in tracks}
            words = []

            def progress(value):
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
                        words.append(dict(word, speaker=track["speaker"], _order=value["trackOrder"]))
                    words.sort(key=lambda w: (w["startMs"], w["_order"]))
                    body = [{k: v for k, v in word.items() if k != "_order"} for word in words]
                    atomic_json(
                        directory / "transcript.json",
                        dict(format="cassini.words.v1", language=value["language"], items=body),
                    )
                update_metadata(directory, etaMinutes=value["etaMinutes"], speakers=list(speakers.values()))
                self.changed(directory)

            if self.client is None:
                self.client = self.client_factory(self.bundle, timeout=120)
                self.client.start()
            complete = self.client.process_tracks(tracks, progress)
            for diagnostic in complete.get("diagnostics", []):
                LOG.warning("Transcription diagnostic: %s", diagnostic)
            update_metadata(
                directory,
                transcript=dict(status="done", reference=REFERENCE),
                etaMinutes=None,
                error=None,
                speakers=list(speakers.values()),
            )
            self.changed(directory)

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.client:
            self.client.close()
            self.client = None
