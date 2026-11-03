# SPDX-License-Identifier: GPL-3.0-or-later
"""D60 (R83): the test recording, 8 s of both native streams, then deleted.

It records through the real NativeRecorder path (each side opens on its own
within the 3 s bound, D58 refusals) into a private cache folder,
$XDG_CACHE_HOME/cassini-talk/test/<uuid>/. The recorder is never registered
as a capture, so no Library row, indicator, notification or transcription
sees it. Nothing is transcribed, named or analysed; a real Start discards it.
"""

import logging
import math
import os
import shutil
import sys
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from gi.repository import GLib, GObject

from .capture_guard import recorder_busy

DURATION_MS = 8000
KINDS = ("microphone", "system")
SIDES = {"microphone": "me", "system": "others"}
STREAMS = {"microphone": "common.yourMicrophone", "system": "recordFirst.sound.title"}
WORKING, SILENT, REFUSED = "working", "silent", "refused"
# recognition_fallback's ACTIVE_DBFS, FRAME (512 at 16 kHz) and MIN_RUN_FRAMES;
# that module needs numpy, so the values are repeated here and tested equal.
ACTIVE_DBFS, FRAME_MS, MIN_RUN_FRAMES = -50.0, 32, 5
# How long an exit waits for a discarded test's recorder: past NativeRecorder's
# 10 s stop deadline. A folder still in use then is left for the next sweep.
EXIT_BOUND_MS = 15_000
LOG = logging.getLogger(__name__)


def cache_root():
    """$XDG_CACHE_HOME/cassini-talk; a relative XDG_CACHE_HOME is ignored (XDG)."""
    base = os.environ.get("XDG_CACHE_HOME")
    base = Path(base) if base and os.path.isabs(base) else Path.home() / ".cache"
    return base / "cassini-talk"


def folders_root():
    return cache_root() / "test"


def _checked_root(root):
    """The test root, only when it is a real cassini-talk/test folder (no links)."""
    root = Path(root)
    if root.name != "test" or root.parent.name != "cassini-talk":
        raise ValueError(f"Not the test recording folder: {root}")
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError(f"The test recording folder is a link: {root}")
    return root


def _test_folder(folder, root):
    """Whether folder is a direct, non-link <uuid> child of the test root."""
    folder = Path(folder)
    try:
        UUID(folder.name)
    except ValueError:
        return False
    return folder.parent == root and not folder.is_symlink()


def make_folder(root):
    """Create root/<uuid>, with root and its cassini-talk parent, all 0700."""
    root = Path(root)
    root.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for level in (root.parent, root):
        if level.is_symlink():  # Never create anything through a link.
            raise ValueError(f"The test recording folder is a link: {level}")
        level.mkdir(mode=0o700, exist_ok=True)
    root = _checked_root(root)
    os.chmod(root, 0o700)
    folder = root / str(uuid4())
    folder.mkdir(mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def remove(folder, root):
    """Delete one test folder; anything but a <uuid> child of the root is refused."""
    root = _checked_root(root)
    if not _test_folder(folder, root):
        raise ValueError(f"Not a test recording folder: {folder}")
    if Path(folder).exists():
        shutil.rmtree(folder)


def sweep(root):
    """Remove leftovers (a crash) under the test root only; links are never followed."""
    root = Path(root)
    if not root.exists():
        return 0
    root = _checked_root(root)
    removed = 0
    for child in root.iterdir():
        if _test_folder(child, root) and child.is_dir():
            shutil.rmtree(child)
            removed += 1
        else:
            LOG.warning("Left an unexpected entry in the test recording folder: %s", child.name)
    return removed


def judge(path):
    """WORKING when some audio is above the activity floor (D52.4, G10), else SILENT.

    ActivityMeter's rule in plain Python, so a profile without numpy can test
    too: mean-removed RMS over 32 ms frames, loud at -50 dBFS (its rounded
    level code), active in runs of at least 160 ms. The floor is -50 dBFS
    alone, as for any track under 10 s: a side that takes up to the 3 s open
    bound makes the other's track longer than the 8 s test, and must not
    switch it to the floor + 12 dB rule.
    """
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError("The test recording is not PCM16")
        rate, channels = source.getframerate(), source.getnchannels()
        size = rate * FRAME_MS // 1000
        run = 0
        while data := source.readframes(size):
            samples = array("h")
            samples.frombytes(data)
            if sys.byteorder == "big":
                samples.byteswap()
            if channels > 1:
                samples = [sum(samples[i : i + channels]) / channels for i in range(0, len(samples), channels)]
            if len(samples) < size:
                break  # A partial last frame, as ActivityMeter keeps it unmeasured.
            mean = sum(samples) / size
            power = sum((x - mean) ** 2 for x in samples) / size
            level = math.sqrt(power) / 32768
            loud = level > 0 and round(120 + 20 * math.log10(level)) - 120 >= ACTIVE_DBFS
            run = run + 1 if loud else 0
            if run >= MIN_RUN_FRAMES:
                return WORKING
    return SILENT


@dataclass(frozen=True)
class Outcome:
    state: str  # WORKING, SILENT or REFUSED
    refusal: dict | None = None  # The D58 entry when refused.

    @property
    def code(self):
        return self.refusal["code"] if self.refusal else None


def judge_streams(directory, tracks, refusals):
    """One Outcome per stream: a track that opened is judged, any other is refused.

    A side without its own refusal (the start failed before any side opened)
    is deviceUnavailable: permissionDenied comes only from a known refusal.
    """
    from .refusals import entry

    outcomes = {}
    for kind in KINDS:
        track = next((t for t in tracks if t.get("kind") == kind), None)
        if directory is not None and track is not None:
            try:
                outcomes[kind] = Outcome(judge(Path(directory) / track["file"]) if track["frames"] > 0 else SILENT)
            except (OSError, ValueError, EOFError) as error:
                LOG.warning("Could not read the test recording of %s: %s", kind, error)
                outcomes[kind] = Outcome(REFUSED, entry(0, SIDES[kind], "deviceUnavailable", message=str(error)))
            continue
        refusal = next((r for r in refusals if r.get("side") == SIDES[kind]), None)
        outcomes[kind] = Outcome(REFUSED, refusal or entry(0, SIDES[kind], "deviceUnavailable"))
    return outcomes


def result_line(kind, outcome):
    """The result line of one stream, in catalog copy."""
    from .refusals import line
    from .strings import text

    if outcome.state == REFUSED:
        key, _origin = line(outcome.refusal)
        return text(key, source=text(STREAMS[kind]))  # Named as on the other lines.
    identifier = "test.result.working" if outcome.state == WORKING else "test.result.silent"
    return text(identifier, stream=text(STREAMS[kind]))


class _Run:
    def __init__(self, folder, recorder):
        self.folder, self.recorder = folder, recorder
        self.directory = None  # The recorder's Recording-<id> folder once started.
        self.tracks = []


class SoundCheck(GObject.Object):
    """One test at a time: idle -> running -> judging -> result.

    changed: on every state change; finished: when a result is ready.
    cancel() and close() both discard: the recorder stops and its result is
    ignored, and the folder goes once it is made and the recorder has
    finished with it. Until then the application is held, so an exit waits.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "finished": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(
        self, recorder=None, *, root=None, duration_ms=DURATION_MS, application=None, exit_bound_ms=EXIT_BOUND_MS
    ):
        super().__init__()
        if recorder is None:
            from .native_recorder import NativeRecorder

            recorder = NativeRecorder
        self._recorder_factory = recorder
        self._root = Path(root) if root is not None else None
        self.duration_ms = duration_ms
        self.state = "idle"
        self.outcomes = None
        self._run = None
        self._timer = 0
        self._discarded = set()  # Discarded runs not yet deleted.
        self._application = application  # Held while any is.
        self._held = False
        self._waiters = []  # flush() callbacks, run on the release.
        self.exit_bound_ms = exit_bound_ms
        self._bound = 0
        self._closed = False
        # One worker: a sweep, a folder's creation, judging and deletion run in order.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-sound-check")

    @property
    def root(self):
        return self._root if self._root is not None else folders_root()

    @property
    def running(self):
        return self.state in ("running", "judging")

    @property
    def busy(self):
        """A test runs, or a discarded one is still stopping or being deleted."""
        return self.running or bool(self._discarded)

    @property
    def held(self):
        """The application is held until every discarded test is deleted."""
        return self._held

    @property
    def closed(self):
        """After shutdown() (app exit) no test can start."""
        return self._closed

    @property
    def folder(self):
        return self._run.folder if self._run else None

    def sweep(self):
        """Remove leftovers off GTK, before any later test's folder is made."""
        future = self._executor.submit(sweep, self.root)
        future.add_done_callback(lambda f: f.exception() and LOG.warning("Could not sweep: %s", f.exception()))
        return future

    def start(self, *, test=False):
        if self.busy or self._closed:
            raise RuntimeError("A test recording is already running")
        self._discard()  # Test again: the previous result goes first.
        recorder = self._recorder_factory()
        run = self._run = _Run(None, recorder)
        recorder.connect("started", self._started, run)
        recorder.connect("finished", self._finished, run)
        self.state, self.outcomes = "running", None
        future = self._executor.submit(make_folder, self.root)
        future.add_done_callback(lambda f: GLib.idle_add(self._made, f, run, test))
        self.emit("changed")

    def _made(self, future, run, test):
        try:
            run.folder = future.result()
        except (OSError, ValueError) as error:
            LOG.warning("Could not make the test recording folder: %s", error)
            run.recorder.close()
            if run is self._run:
                self._result({kind: self._unavailable(kind, str(error)) for kind in KINDS})
            else:
                self._done(run)
            return False
        if run is not self._run:
            run.recorder.close()  # Discarded before it started.
            self._delete(run)
            return False
        run.recorder.start(run.folder, test=test, observe_output=False)
        return False

    @staticmethod
    def _unavailable(kind, message):
        from .refusals import entry

        return Outcome(REFUSED, entry(0, SIDES[kind], "deviceUnavailable", message=message))

    def _started(self, _recorder, _path, run):
        # A Stop during the start can still see a side commit (late started).
        if run is self._run and self.state == "running" and not self._timer:
            self._timer = GLib.timeout_add(self.duration_ms, self._elapsed)

    def _elapsed(self):
        self._timer = 0
        if self._run is not None and self.state == "running":
            self._run.recorder.stop()
        return False

    def _finished(self, recorder, path, run):
        recorder.close()
        if run is not self._run:
            self._delete(run)
            return
        self._stop_timer()
        session = recorder.session
        tracks = [dict(t) for t in session.manifest["tracks"]] if session is not None and path else []
        run.directory, run.tracks = (Path(path), tracks) if path else (None, [])
        future = self._executor.submit(judge_streams, run.directory, tracks, list(recorder.refusals))
        future.add_done_callback(lambda f: GLib.idle_add(self._judged, f, run))
        # Queued after the judging: a cancel from here deletes the folder once it is read.
        self.state = "judging"
        self.emit("changed")

    def _judged(self, future, run):
        if run is not self._run:
            return False
        try:
            outcomes = future.result()
        except Exception as error:  # Judging never leaves the test running.
            LOG.warning("Could not judge the test recording: %s", error)
            outcomes = {kind: self._unavailable(kind, str(error)) for kind in KINDS}
        self._result(outcomes)
        return False

    def _result(self, outcomes):
        self.state, self.outcomes = "result", outcomes
        self.emit("changed")
        self.emit("finished")

    def tracks(self):
        """The result's tracks and duration for FolderPlayer.load_tracks (both together)."""
        from .library import CaptureTrack

        run = self._run
        if self.state != "result" or run is None or run.directory is None:
            return [], 0
        tracks = [
            CaptureTrack(
                path=run.directory / t["file"],
                frames=t["frames"],
                sample_rate=t["sampleRate"],
                start_offset_ms=t.get("startOffsetMs", 0),
                ended=True,
                kind=t["kind"],
                id=t["id"],
            )
            for t in run.tracks
            if t["frames"] > 0
        ]
        duration = max((t.start_offset_ms + t.frames * 1000 / t.sample_rate for t in tracks), default=0)
        return tracks, duration

    def cancel(self):
        """Stop a running test and delete it; its result is never shown."""
        self._discard()

    def close(self):
        """Close the result (Done) or a running test: either is deleted."""
        self._discard()

    def shutdown(self):
        """App exit: discard. The hold keeps GTK running until every discarded
        test, also one discarded just before (the last window closed), is
        deleted; after exit_bound_ms the hold goes and the next start sweeps
        what is left."""
        if self._closed:
            return
        self._discard()
        self._closed = True
        if self._discarded:
            self._bound = GLib.timeout_add(self.exit_bound_ms, self._expired)
        else:
            self._executor.shutdown(wait=False)

    def flush(self, callback):
        """Whether a discarded test is still being deleted; if so, callback
        runs once it is and the hold is released (Quit waits for it)."""
        if not self._held:
            return False
        self._waiters.append(callback)
        return True

    def _expired(self):
        self._bound = 0
        for run in self._discarded:
            LOG.warning("Left the test recording %s for the next start's sweep", run.folder or "still being made")
        self._executor.shutdown(wait=False)  # Deletions already queued still run.
        self._release()
        return False

    def _discard(self):
        run, changed = self._run, self.state != "idle"
        self._stop_timer()
        self._run, self.outcomes, self.state = None, None, "idle"
        if run is not None:
            # busy and the hold stay until the folder is gone (_done).
            self._discarded.add(run)
            self._hold()
            if run.folder is None:
                pass  # Still being made: _made deletes it.
            elif recorder_busy(run.recorder):
                run.recorder.stop()  # _finished deletes it.
            else:
                self._delete(run)
        if changed:
            self.emit("changed")

    def _delete(self, run):
        """Delete off GTK, never on it, after any judging of it on the same worker."""
        try:
            future = self._executor.submit(remove, run.folder, self.root)
        except RuntimeError:  # Finished past the exit bound.
            LOG.warning("Left the test recording %s for the next start's sweep", run.folder)
            return
        future.add_done_callback(lambda f: GLib.idle_add(self._deleted, f, run))

    def _deleted(self, future, run):
        if error := future.exception():
            LOG.warning("Could not delete the test recording %s: %s", run.folder, error)
        self._done(run)
        return False

    def _done(self, run):
        self._discarded.discard(run)
        self.emit("changed")
        if self._discarded:
            return
        if self._closed:
            self._executor.shutdown(wait=False)
            if self._bound:
                GLib.source_remove(self._bound)
                self._bound = 0
        self._release()

    def _hold(self):
        if not self._held:
            self._held = True
            if self._application is not None:
                self._application.hold()

    def _release(self):
        if not self._held:
            return
        self._held = False
        if self._application is not None:
            self._application.release()
        waiters, self._waiters = self._waiters, []
        for callback in waiters:
            callback()

    def _stop_timer(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
