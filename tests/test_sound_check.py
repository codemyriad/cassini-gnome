# SPDX-License-Identifier: GPL-3.0-or-later
"""D60 (R83): the test recording's judge, lifetime and private folder.

A scripted recorder stands in for the devices; every wait is on a GLib
event (a signal or an idle callback), with a timeout only as a bound.
"""

import math
import os
import random
import stat
import sys
import threading
import types
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("GStreamer typelib is unavailable", allow_module_level=True)
from gi.repository import GLib, GObject, Gst  # noqa: E402
from test_voice_application import application_class  # noqa: E402,F401

from cassini_talk import sound_check  # noqa: E402
from cassini_talk.i18n import using_language  # noqa: E402
from cassini_talk.refusals import entry  # noqa: E402
from cassini_talk.sound_check import (  # noqa: E402
    REFUSED,
    SILENT,
    WORKING,
    Outcome,
    SoundCheck,
    judge,
    judge_streams,
    make_folder,
    remove,
    result_line,
    sweep,
)

Gst.init(None)
RATE = 48000


def spin_until(predicate, timeout=10):
    """Iterate the main context until predicate holds; the timeout is a bound only."""
    context = GLib.MainContext.default()
    expired = []
    guard = GLib.timeout_add(int(timeout * 1000), lambda: expired.append(True) or False)
    try:
        while not predicate() and not expired:
            context.iteration(True)
    finally:
        if not expired:
            GLib.source_remove(guard)
    assert predicate(), "Timed out waiting for the test recording"


def write_wav(path, samples):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        pcm = array("h", (max(-32768, min(32767, round(x * 32767))) for x in samples))
        if sys.byteorder == "big":
            pcm.byteswap()
        output.writeframes(pcm.tobytes())
    return len(samples)


def tone(dbfs, seconds=2.0, frequency=440):
    peak = 10 ** (dbfs / 20) * math.sqrt(2)  # RMS at dbfs
    return [peak * math.sin(2 * math.pi * frequency * n / RATE) for n in range(int(seconds * RATE))]


def noise(dbfs, seconds=2.0):
    rng = random.Random(7)
    return [10 ** (dbfs / 20) * rng.gauss(0, 1) for _ in range(int(seconds * RATE))]


# The judge: D52.4/G10's activity floor (-50 dBFS alone below 10 s of audio).


def test_a_tone_is_working(tmp_path):
    write_wav(tmp_path / "a.wav", tone(-20))
    assert judge(tmp_path / "a.wav") == WORKING


def test_a_quiet_tone_above_the_floor_is_working(tmp_path):
    write_wav(tmp_path / "a.wav", tone(-40))
    assert judge(tmp_path / "a.wav") == WORKING


def test_digital_silence_is_no_sound_heard(tmp_path):
    write_wav(tmp_path / "a.wav", [0.0] * (2 * RATE))
    assert judge(tmp_path / "a.wav") == SILENT


def test_noise_below_the_floor_is_no_sound_heard(tmp_path):
    write_wav(tmp_path / "a.wav", noise(-62))
    assert judge(tmp_path / "a.wav") == SILENT


def test_a_click_shorter_than_an_active_run_is_no_sound_heard(tmp_path):
    samples = [0.0] * (2 * RATE)
    samples[RATE : RATE + RATE // 20] = tone(-10, 0.05)  # 50 ms: under the 160 ms run
    write_wav(tmp_path / "a.wav", samples)
    assert judge(tmp_path / "a.wav") == SILENT


@pytest.mark.parametrize("signal", [tone(-20, 11), noise(-30, 11)], ids=["tone", "noise"])
def test_a_steady_sound_over_10_s_is_still_working(tmp_path, signal):
    # The other side took the 3 s open bound: this track is about 11 s.
    write_wav(tmp_path / "a.wav", signal)
    assert judge(tmp_path / "a.wav") == WORKING


def test_a_sound_exactly_at_minus_50_dbfs_is_working(tmp_path):
    # The oracle's loud frame is level >= ACTIVE_DBFS, so the limit itself counts.
    write_wav(tmp_path / "a.wav", tone(-50))
    assert judge(tmp_path / "a.wav") == WORKING


def test_the_judge_constants_match_the_corpus_and_the_oracle():
    """Without numpy too: sound_check's repeated G10 rule against corpus.json and activity.py."""
    import importlib.util
    import json

    root = Path(__file__).parents[1] / "browser-protocol/conformance/multistream"
    constants = json.loads((root / "corpus.json").read_text())["constants"]
    spec = importlib.util.spec_from_file_location("shared_activity", root / "activity.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    assert sound_check.ACTIVE_DBFS == constants["ACTIVE_DBFS"] == shared.ACTIVE_DBFS
    assert sound_check.FRAME_MS == shared.FRAME_MS == shared.FRAME_16K * 1000 // 16000 == 32
    assert sound_check.MIN_RUN_FRAMES == shared.MIN_RUN_FRAMES == constants["MIN_RUN_MS"] // sound_check.FRAME_MS


@pytest.mark.parametrize(
    "signal",
    [tone(-20), tone(-49), tone(-50), tone(-51), noise(-40), noise(-62), [0.0] * RATE],
    ids=["tone-20", "tone-49", "tone-50", "tone-51", "noise-40", "noise-62", "silence"],
)
def test_the_judge_agrees_with_the_activity_meter(tmp_path, signal):
    """sound_check repeats ActivityMeter's rule without numpy; both give the same answer."""
    pytest.importorskip("numpy")
    from cassini_talk import recognition_fallback
    from cassini_talk.speech_pipeline import resampled_chunks

    assert (sound_check.ACTIVE_DBFS, sound_check.MIN_RUN_FRAMES) == (
        recognition_fallback.ACTIVE_DBFS,
        recognition_fallback.MIN_RUN_FRAMES,
    )
    assert sound_check.FRAME_MS == recognition_fallback.FRAME * 1000 // recognition_fallback.RATE
    write_wav(tmp_path / "a.wav", signal)
    meter = recognition_fallback.ActivityMeter()
    meter.threshold_db = lambda: (None, recognition_fallback.ACTIVE_DBFS)
    for _block in meter.wrap(resampled_chunks(tmp_path / "a.wav")):
        pass
    assert judge(tmp_path / "a.wav") == (WORKING if any(meter.active()) else SILENT)


def test_quiet_noise_over_10_s_is_still_no_sound_heard(tmp_path):
    write_wav(tmp_path / "a.wav", noise(-62, 11))
    assert judge(tmp_path / "a.wav") == SILENT


def test_each_stream_is_judged_on_its_own_track(tmp_path):
    write_wav(tmp_path / "track-microphone.wav", tone(-20))
    write_wav(tmp_path / "track-system.wav", [0.0] * RATE)
    tracks = [
        dict(kind="system", file="track-system.wav", frames=RATE),
        dict(kind="microphone", file="track-microphone.wav", frames=2 * RATE),
    ]
    assert judge_streams(tmp_path, tracks, []) == dict(microphone=Outcome(WORKING), system=Outcome(SILENT))


def test_a_refused_stream_keeps_its_d58_code(tmp_path):
    write_wav(tmp_path / "track-system.wav", tone(-20))
    denied = entry(0, "me", "permissionDenied", message="Portal denied")
    outcomes = judge_streams(tmp_path, [dict(kind="system", file="track-system.wav", frames=2 * RATE)], [denied])
    assert outcomes["microphone"] == Outcome(REFUSED, denied) and outcomes["microphone"].code == "permissionDenied"
    assert outcomes["system"] == Outcome(WORKING)
    unavailable = entry(0, "others", "deviceUnavailable", message="No such entity")
    outcomes = judge_streams(None, [], [denied, unavailable])
    assert (outcomes["microphone"].code, outcomes["system"].code) == ("permissionDenied", "deviceUnavailable")


def test_a_side_with_no_track_and_no_refusal_is_device_unavailable_never_permission_denied():
    outcomes = judge_streams(None, [], [])
    assert {o.state for o in outcomes.values()} == {REFUSED}
    assert {o.code for o in outcomes.values()} == {"deviceUnavailable"}


def test_a_track_without_frames_is_no_sound_heard(tmp_path):
    outcomes = judge_streams(tmp_path, [dict(kind="microphone", file="track-microphone.wav", frames=0)], [])
    assert outcomes["microphone"] == Outcome(SILENT)


def test_result_lines_use_the_catalog():
    with using_language("en"):
        assert result_line("microphone", Outcome(WORKING)) == "Your microphone: working"
        assert result_line("system", Outcome(SILENT)) == "This computer’s sound: no sound heard"
        refused = Outcome(REFUSED, entry(0, "me", "deviceUnavailable"))
        assert result_line("microphone", refused) == "Your microphone: couldn’t be started."
        denied = Outcome(REFUSED, entry(0, "others", "permissionDenied"))
        assert result_line("system", denied) == "This computer’s sound: access wasn’t allowed."


# The private folder.


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_the_cache_root_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert sound_check.folders_root() == tmp_path / "cache/cassini-talk/test"
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert sound_check.cache_root() == Path.home() / ".cache/cassini-talk"


def test_each_level_of_the_folder_is_private(tmp_path):
    old = os.umask(0o022)
    try:
        folder = make_folder(tmp_path / "cache/cassini-talk/test")
    finally:
        os.umask(old)
    assert folder.parent == tmp_path / "cache/cassini-talk/test"
    assert [mode(p) for p in (folder, folder.parent, folder.parent.parent)] == [0o700] * 3


def test_a_linked_cache_folder_is_refused(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache/cassini-talk").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        make_folder(tmp_path / "cache/cassini-talk/test")
    assert not any((tmp_path / "elsewhere").iterdir())  # Nothing made through the link.
    (tmp_path / "elsewhere/test").mkdir()
    keep = tmp_path / "elsewhere/test" / str(uuid4())
    keep.mkdir()
    with pytest.raises(ValueError):
        sweep(tmp_path / "cache/cassini-talk/test")
    assert keep.is_dir()


def test_sweep_removes_only_test_folders_and_never_follows_links(tmp_path):
    root = tmp_path / "cache/cassini-talk/test"
    leftover = make_folder(root)
    (leftover / "Recording-x").mkdir()
    (leftover / "Recording-x/track-system.wav").write_bytes(b"pcm")
    outside = tmp_path / "Documents" / str(uuid4())
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("mine")
    link = root / str(uuid4())
    link.symlink_to(outside)
    (root / "notes.txt").write_text("not a test folder")
    sibling = tmp_path / "cache/cassini-talk" / str(uuid4())
    sibling.mkdir()
    assert sweep(root) == 1
    assert not leftover.exists()
    assert (outside / "keep.txt").read_text() == "mine" and link.is_symlink()
    assert (root / "notes.txt").exists() and sibling.is_dir()


def test_remove_refuses_anything_but_a_test_folder(tmp_path):
    root = tmp_path / "cache/cassini-talk/test"
    root.mkdir(parents=True)
    for victim in (tmp_path / str(uuid4()), root / "named", root.parent / str(uuid4())):
        victim.mkdir()
        with pytest.raises(ValueError):
            remove(victim, root)
        assert victim.is_dir()
    other = tmp_path / "cache/other/test"
    other.mkdir(parents=True)
    with pytest.raises(ValueError):
        remove(other / str(uuid4()), other)
    with pytest.raises(ValueError):
        sweep(other)


# The controller, with a scripted recorder.


class FakeRecorder(GObject.Object):
    __gsignals__ = {
        name: (GObject.SignalFlags.RUN_LAST, None, args)
        for name, args in (("started", (str,)), ("finished", (str,)), ("error", (str,)), ("changed", ()))
    }
    made = []

    def __init__(self):
        super().__init__()
        self.pipeline = self.session = self.parent = None
        self.starting = self.stopping = self._finishing = False
        self.refusals, self.stops, self.closed, self.kwargs = [], 0, False, None
        self._stop_requested = False
        FakeRecorder.made.append(self)

    def start(self, parent, **kwargs):
        self.parent, self.kwargs, self.starting = Path(parent), kwargs, True

    def open(self, microphone=None, system=None):
        """Sides commit: each given signal becomes that side's WAV."""
        directory = self.parent / f"Recording-{uuid4()}"
        directory.mkdir(mode=0o700)
        tracks = []
        for kind, samples in (("microphone", microphone), ("system", system)):
            if samples is not None:
                frames = write_wav(directory / f"track-{kind}.wav", samples)
                tracks.append(
                    dict(id=kind, kind=kind, file=f"track-{kind}.wav", frames=frames, sampleRate=RATE, startOffsetMs=0)
                )
        self.session = SimpleNamespace(directory=directory, manifest=dict(tracks=tracks))
        self.starting, self.pipeline = False, object()
        self.emit("started", str(directory))
        if self._stop_requested:
            self.stop()  # Like NativeRecorder: a side that commits as a Stop comes still starts.

    def refuse(self, *refusals):
        """Nothing opened: no recording, the refusals only."""
        self.starting, self.refusals = False, list(refusals)
        self.emit("finished", "")

    def stop(self):
        self.stops += 1
        if self.starting:
            self._stop_requested = True
            GLib.idle_add(self._cancelled)
        elif self.pipeline is not None and not self.stopping:
            self.stopping = True

            def done():
                self.pipeline, self.stopping = None, False
                self.emit("finished", str(self.session.directory))
                return False

            GLib.idle_add(done)

    def _cancelled(self):
        if self.starting:  # No side opened before the Stop.
            self.starting = False
            self.emit("finished", "")
        return False

    def close(self):
        assert self.pipeline is None and not self.starting and not self.stopping
        self.closed = True


class PreparingRecorder(FakeRecorder):
    """NativeRecorder's start: _prepare writes Recording-<id>/session.json on its
    own worker, and a Stop while it runs takes effect once it returns."""

    def start(self, parent, **kwargs):
        super().start(parent, **kwargs)
        self.entered, self.release = threading.Event(), threading.Event()
        self.preparing = threading.Thread(target=self._prepare)
        self.preparing.start()

    def _prepare(self):
        self.entered.set()
        assert self.release.wait(10), "The test did not release the preparation"
        directory = self.parent / f"Recording-{uuid4()}"
        directory.mkdir(mode=0o700, parents=True)  # As RecordingSession does.
        (directory / "session.json").write_text("{}")
        GLib.idle_add(self._prepared)

    def _prepared(self):
        if self._stop_requested:
            self.starting = False
            self.emit("finished", "")
        return False

    def stop(self):
        self.stops += 1
        self._stop_requested = True


class Application:
    """Gio.Application's hold count; each release notes what the test root holds."""

    def __init__(self, root):
        self.root, self.holds, self.left = root, 0, []

    def hold(self):
        self.holds += 1

    def release(self):
        assert self.holds > 0, "Released more than held"
        self.holds -= 1
        self.left.append(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))


def exited(application):
    """GTK runs only while the app is held: iterate until the release, then never again."""
    spin_until(lambda: application.holds == 0)


@pytest.fixture
def check(tmp_path):
    FakeRecorder.made.clear()
    check = SoundCheck(FakeRecorder, root=tmp_path / "cache/cassini-talk/test", duration_ms=20)
    check.signals = []
    check.connect("changed", lambda c: c.signals.append(("changed", c.state)))
    check.connect("finished", lambda c: c.signals.append(("finished", c.state)))
    yield check
    check.shutdown()
    spin_until(lambda: not check.held)


@pytest.fixture
def off_gtk(monkeypatch):
    """Every deletion's thread: none may run on GTK (the main thread)."""
    threads = []

    def removing(folder, root):
        threads.append(threading.current_thread())
        return remove(folder, root)

    monkeypatch.setattr(sound_check, "remove", removing)
    return threads


def started(check):
    check.start()
    spin_until(lambda: FakeRecorder.made and FakeRecorder.made[-1].parent is not None)
    return FakeRecorder.made[-1]


def gone(check, folder):
    spin_until(lambda: not check.busy)
    return not folder.exists()


def test_a_test_records_both_streams_privately_then_judges_them(check):
    recorder = started(check)
    folder = recorder.parent
    assert folder.parent == check.root and mode(folder) == 0o700
    assert recorder.kwargs == dict(test=False, observe_output=False)  # No mute observer writes.
    assert check.running and check.busy
    recorder.open(microphone=tone(-20), system=[0.0] * RATE)
    spin_until(lambda: check.state == "result")
    assert recorder.stops == 1 and recorder.closed  # The 8 s timer stopped it.
    assert check.outcomes == dict(microphone=Outcome(WORKING), system=Outcome(SILENT))
    assert check.signals[-1] == ("finished", "result")
    tracks, duration = check.tracks()
    assert [t.kind for t in tracks] == ["microphone", "system"] and duration == 2000
    assert folder.exists()
    check.close()
    assert check.state == "idle" and check.outcomes is None and check.busy
    assert gone(check, folder)


def test_cancel_while_recording_stops_deletes_and_never_shows_a_result(check):
    recorder = started(check)
    folder = recorder.parent
    check.duration_ms = 60_000
    recorder.open(microphone=tone(-20), system=tone(-20))
    check.cancel()
    assert check.state == "idle" and recorder.stops == 1
    assert gone(check, folder) and recorder.closed
    assert ("finished", "result") not in check.signals and check.outcomes is None


def test_cancel_while_judging_never_shows_the_result(check):
    recorder = started(check)
    folder = recorder.parent
    check.connect("changed", lambda c: c.state == "judging" and c.cancel())
    recorder.open(microphone=tone(-20), system=tone(-20))
    spin_until(lambda: ("changed", "judging") in check.signals and not check.busy)
    assert check.state == "idle" and check.outcomes is None and not folder.exists()
    assert ("finished", "result") not in check.signals


def test_cancel_while_the_sides_open_deletes_the_folder(check):
    recorder = started(check)
    folder = recorder.parent
    check.cancel()
    assert recorder.stops == 1
    assert gone(check, folder) and recorder.closed
    assert ("finished", "result") not in check.signals


def test_cancel_before_the_folder_exists_never_starts_the_recorder(check):
    check.start()
    check.cancel()
    assert check.busy  # Its folder is still being made.
    spin_until(lambda: not check.busy)
    (recorder,) = FakeRecorder.made
    assert recorder.parent is None and recorder.closed
    assert not any(check.root.iterdir())


def test_a_late_started_after_cancel_is_ignored(check):
    recorder = started(check)
    check.cancel()
    recorder.open(microphone=tone(-20), system=tone(-20))  # A side committed as the Stop came.
    assert not check._timer and check.state == "idle"
    spin_until(lambda: not check.busy)
    assert not any(check.root.iterdir()) and ("finished", "result") not in check.signals


def test_nothing_opened_shows_each_refusal(check):
    recorder = started(check)
    recorder.refuse(entry(0, "me", "deviceUnavailable", message="No such entity"))
    spin_until(lambda: check.state == "result")
    assert check.outcomes["microphone"].code == "deviceUnavailable"
    assert check.outcomes["system"] == Outcome(REFUSED, entry(0, "others", "deviceUnavailable"))
    assert check.tracks() == ([], 0)
    folder = recorder.parent
    check.close()
    assert gone(check, folder)


def test_one_test_at_a_time_and_test_again_deletes_the_last_result(check):
    recorder = started(check)
    with pytest.raises(RuntimeError):
        check.start()
    recorder.open(microphone=tone(-20), system=tone(-20))
    spin_until(lambda: check.state == "result")
    first = recorder.parent
    again = started(check)
    assert again is not recorder and again.parent != first
    spin_until(lambda: not first.exists())
    assert check.running and again.parent.exists()


def test_a_discard_holds_the_app_until_its_folder_is_gone(tmp_path, off_gtk):
    FakeRecorder.made.clear()
    application = Application(tmp_path / "cache/cassini-talk/test")
    check = SoundCheck(FakeRecorder, root=application.root, application=application, duration_ms=60_000)
    recorder = started(check)
    recorder.open(microphone=tone(-20), system=tone(-20))
    check.cancel()
    assert application.holds == 1 and check.held and recorder.parent.exists()
    exited(application)
    assert application.left == [[]] and recorder.closed and not check.held
    assert off_gtk and threading.main_thread() not in off_gtk
    check.shutdown()
    assert application.holds == 0 and not check.flush(lambda: None)  # Nothing left to wait for.


def test_quit_holds_the_app_until_the_recorder_finishes_then_deletes(tmp_path, off_gtk):
    FakeRecorder.made.clear()
    application = Application(tmp_path / "cache/cassini-talk/test")
    check = SoundCheck(FakeRecorder, root=application.root, application=application, duration_ms=60_000)
    recorder = started(check)
    recorder.open(microphone=tone(-20), system=tone(-20))
    folder = recorder.parent
    check.shutdown()
    quitting = []
    assert check.flush(lambda: quitting.append(recorder.closed))
    assert recorder.stops == 1 and recorder.stopping and folder.exists(), "Never deleted while the recorder writes"
    assert application.holds == 1 and not quitting
    exited(application)
    assert application.left == [[]] and quitting == [True]
    assert threading.main_thread() not in off_gtk
    with pytest.raises(RuntimeError):
        check.start()


def test_quit_while_the_folder_is_made_waits_for_it(tmp_path, monkeypatch, off_gtk):
    made, release = threading.Event(), threading.Event()

    def gated(root):
        made.set()
        assert release.wait(10), "The test did not release the folder's creation"
        return make_folder(root)

    monkeypatch.setattr(sound_check, "make_folder", gated)
    FakeRecorder.made.clear()
    application = Application(tmp_path / "cache/cassini-talk/test")
    check = SoundCheck(FakeRecorder, root=application.root, application=application)
    check.start()
    assert made.wait(10)
    check.shutdown()  # Quit while the worker makes the folder.
    quitting = []
    assert check.flush(lambda: quitting.append(True)) and application.holds == 1
    release.set()
    exited(application)
    assert application.left == [[]] and quitting == [True]
    (recorder,) = FakeRecorder.made
    assert recorder.parent is None and recorder.closed  # Never started.
    assert off_gtk and threading.main_thread() not in off_gtk


def test_quit_while_the_recorder_prepares_waits_for_it(tmp_path, off_gtk):
    FakeRecorder.made.clear()
    application = Application(tmp_path / "cache/cassini-talk/test")
    check = SoundCheck(PreparingRecorder, root=application.root, application=application)
    check.start()
    spin_until(lambda: FakeRecorder.made and FakeRecorder.made[-1].parent is not None)
    (recorder,) = FakeRecorder.made
    assert recorder.entered.wait(10)
    check.shutdown()  # Quit while the recorder prepares its session.
    check._executor.submit(lambda: None).result()  # The worker ran whatever the quit queued.
    assert recorder.stops == 1 and recorder.parent.exists() and application.holds == 1
    recorder.release.set()
    exited(application)
    recorder.preparing.join(10)
    assert application.left == [[]] and not recorder.parent.exists()
    assert threading.main_thread() not in off_gtk


def test_quit_while_the_real_recorder_prepares_leaves_no_folder(tmp_path, monkeypatch, off_gtk):
    """The real NativeRecorder: its _prepare would make Recording-*/session.json in a deleted folder."""
    from cassini_talk.native_recorder import NativeRecorder

    entered, release = threading.Event(), threading.Event()
    prepare = NativeRecorder._prepare

    def gated(parent):
        entered.set()
        assert release.wait(10), "The test did not release the preparation"
        return prepare(parent)

    monkeypatch.setattr(NativeRecorder, "_prepare", staticmethod(gated))
    application = Application(tmp_path / "cache/cassini-talk/test")
    check = SoundCheck(root=application.root, application=application)
    check.start(test=True)
    spin_until(entered.is_set)
    folder = check.folder
    check.shutdown()
    assert application.holds == 1 and folder.exists()
    release.set()
    exited(application)
    assert application.left == [[]] and not folder.exists()
    assert threading.main_thread() not in off_gtk


def test_a_recorder_stuck_past_the_exit_bound_leaves_its_folder_for_the_sweep(tmp_path, caplog, off_gtk):
    FakeRecorder.made.clear()
    application = Application(tmp_path / "cache/cassini-talk/test")
    check = SoundCheck(
        FakeRecorder, root=application.root, application=application, duration_ms=60_000, exit_bound_ms=50
    )
    recorder = started(check)
    recorder.open(microphone=tone(-20), system=tone(-20))
    recorder.stop = lambda: setattr(recorder, "stopping", True)  # It never finishes.
    folder = recorder.parent
    check.shutdown()
    quitting = []
    assert check.flush(lambda: quitting.append(True))
    exited(application)  # The bound released the hold.
    assert quitting == [True] and folder.exists() and application.left[0]
    assert f"Left the test recording {folder} for the next start's sweep" in caplog.messages
    # A finish after the exit bound deletes nothing on GTK: the next start sweeps it.
    recorder.stopping, recorder.pipeline = False, None
    recorder.emit("finished", str(recorder.session.directory))
    assert folder.exists() and not off_gtk and application.holds == 0
    assert sweep(application.root) == 1 and not folder.exists()


@pytest.mark.parametrize("observe", [True, False])
def test_a_test_recording_starts_no_mute_observer(tmp_path, monkeypatch, observe):
    """observe_output=False: the test writes no meeting.json audio health."""
    from test_native_sides import Sides, close

    from cassini_talk import output_mute
    from cassini_talk.native_recorder import NativeRecorder

    observers = []

    class Observer:
        def __init__(self, *_args, **_kwargs):
            observers.append(self)

        def start(self):
            pass

        def close(self, _at_ms):
            pass

    monkeypatch.setattr(output_mute, "OutputMuteObserver", Observer)
    recorder = NativeRecorder()
    Sides(recorder, dict(microphone=("ok", 0.1), system=("ok", 0.1)))
    started = []
    recorder.connect("started", lambda *_: started.append(True))
    recorder.start(tmp_path, observe_output=observe)
    spin_until(lambda: started)
    assert len(observers) == (1 if observe else 0)
    close(recorder)


def test_the_real_recorder_path_with_tone_sources(tmp_path):
    """The real NativeRecorder and its per-side open, on audiotestsrc."""
    check = SoundCheck(root=tmp_path / "cache/cassini-talk/test", duration_ms=1000)
    finished = []
    check.connect("finished", lambda c: finished.append(c.state))
    check.start(test=True)
    spin_until(lambda: finished, timeout=20)
    assert check.outcomes == dict(microphone=Outcome(WORKING), system=Outcome(WORKING))
    (folder,) = check.root.iterdir()
    (recording,) = folder.iterdir()
    assert sorted(p.name for p in recording.iterdir()) == ["session.json", "track-microphone.wav", "track-system.wav"]
    check.close()
    spin_until(lambda: not check.busy)
    assert not folder.exists()
    check.shutdown()


# The app: a real Start always wins (D60 point 2).


class Order:
    def __init__(self):
        self.events = []  # "test stopped" and "start", in the order they happen.


def running_test(tmp_path, order, application=None):
    root = tmp_path / "cache/cassini-talk/test"
    check = SoundCheck(FakeRecorder, root=root, duration_ms=60_000, application=application)
    check.start()
    spin_until(lambda: FakeRecorder.made and FakeRecorder.made[-1].parent is not None)
    recorder = FakeRecorder.made[-1]
    recorder.open(microphone=tone(-20), system=tone(-20))
    stop = recorder.stop
    recorder.stop = lambda: order.events.append("test stopped") or stop()
    return check, recorder.parent


def fake_app(application, check, order, **values):
    app = SimpleNamespace(
        sound_check=check,
        _record_start_future=None,
        _quitting=False,
        _browser_snapshot={},
        native_captures={},
        get_windows=lambda: [],
        **values,
    )
    for name in ("discard_test_recording", "recording_active", "browser_capture_active", "can_test_recording"):
        setattr(app, name, types.MethodType(getattr(application, name), app))
    return app


def assert_discarded_first(check, folder, order):
    assert check.state == "idle" and order.events[:2] == ["test stopped", "start"], order.events
    spin_until(lambda: not check.busy)
    assert not folder.exists()


def start_app(application, check, order, **values):
    return fake_app(
        application,
        check,
        order,
        recording_failure=None,
        recording_error=None,
        recording_failures=[],
        **values,
    )


@pytest.fixture
def order():
    FakeRecorder.made.clear()
    return Order()


def test_record_discards_a_running_test_before_a_native_start(application_class, tmp_path, order):  # noqa: F811
    check, folder = running_test(tmp_path, order)
    app = start_app(
        application_class,
        check,
        order,
        browser_companion_available=False,
        start_native=lambda: order.events.append("start"),
    )
    application_class.start_recordings(app)
    assert_discarded_first(check, folder, order)


def test_a_companion_start_all_discards_a_running_test(application_class, tmp_path, order):  # noqa: F811
    check, folder = running_test(tmp_path, order)
    app = start_app(
        application_class,
        check,
        order,
        browser_companion_available=True,
        _record_start_generation=0,
        _record_start_group=None,
        hold=lambda: None,
        _record_start_executor=SimpleNamespace(
            submit=lambda *_a: order.events.append("start") or SimpleNamespace(add_done_callback=lambda _c: None)
        ),
    )
    application_class.start_recordings(app)
    assert_discarded_first(check, folder, order)


def test_a_meeting_retry_discards_a_running_test(application_class, tmp_path, order):  # noqa: F811
    check, folder = running_test(tmp_path, order)
    app = start_app(
        application_class,
        check,
        order,
        _record_start_generation=0,
        _record_start_group=None,
        hold=lambda: None,
        _native_changed=lambda: None,
        _record_start_executor=SimpleNamespace(
            submit=lambda *_a: order.events.append("start") or SimpleNamespace(add_done_callback=lambda _c: None)
        ),
    )
    application_class.retry_meeting(app, dict(id="call/4", callGeneration="call-4"))
    assert_discarded_first(check, folder, order)


def test_a_direct_native_start_discards_a_running_test(application_class, tmp_path, order, monkeypatch):  # noqa: F811
    from cassini_talk import native_recorder

    class Recorder(FakeRecorder):
        def start(self, **_kwargs):
            order.events.append("start")

    monkeypatch.setattr(native_recorder, "NativeRecorder", Recorder)
    check, folder = running_test(tmp_path, order)
    app = start_app(application_class, check, order, _native_held=True, _last_native=None)
    for name in ("_native_started", "_native_finished", "_native_error", "_native_changed"):
        setattr(app, name, lambda *_: None)
    application_class.start_native(app)
    assert_discarded_first(check, folder, order)
    assert len(app.native_captures) == 1


def test_an_automatic_browser_start_discards_a_running_test(application_class, tmp_path, order):  # noqa: F811
    check, folder = running_test(tmp_path, order)
    app = start_app(
        application_class,
        check,
        order,
        _last_native=None,
        _permission_sources=set(),
        record_first=None,
        settings_dialog=None,
        comfort=None,
        build_refresh=None,
        windowless_background=SimpleNamespace(refresh=lambda: None),
    )
    application_class._native_changed(app)  # Nothing records yet: the test stays.
    assert check.state == "running" and not order.events
    # The broker started an automatic recording; its snapshot reaches _native_changed.
    app._browser_snapshot = dict(statuses=[dict(recording=True, path="/library/Recording-a")])
    application_class._native_changed(app)
    assert check.state == "idle" and order.events == ["test stopped"]
    spin_until(lambda: not check.busy)
    assert not folder.exists()


def test_record_during_a_test_starts_a_recording_rather_than_stopping(application_class, tmp_path, order):  # noqa: F811
    check, folder = running_test(tmp_path, order)
    app = start_app(
        application_class,
        check,
        order,
        build_refresh=None,
        on_stop_recording=lambda: order.events.append("stop"),
        _activate_current=lambda: SimpleNamespace(),  # The window.
        _record_current=lambda: order.events.append("record"),
    )
    assert not app.recording_active() and not app.can_test_recording()  # A test runs: no second one.
    application_class.on_record(app)
    assert order.events == ["record"]
    check.cancel()
    spin_until(lambda: not check.busy)
    assert app.can_test_recording()
    app._browser_snapshot = dict(statuses=[dict(recording=True)])
    assert not app.can_test_recording()  # Any recording disables the test.


def test_closing_the_last_window_discards_the_test(application_class, tmp_path, order):  # noqa: F811
    check, folder = running_test(tmp_path, order)
    window, other = SimpleNamespace(get_visible=lambda: True), SimpleNamespace(get_visible=lambda: True)
    app = fake_app(application_class, check, order, _closing_background_windows=True)
    app.get_windows = lambda: [window, other]
    application_class.window_closing(app, window)
    assert check.state == "running"  # Another window still shows it.
    app.get_windows = lambda: [window]
    application_class.window_closing(app, window)
    assert check.state == "idle"
    spin_until(lambda: not check.busy)
    assert not folder.exists()


def test_exiting_holds_the_app_until_the_running_test_is_deleted(application_class, tmp_path, order):  # noqa: F811
    application = Application(tmp_path / "cache/cassini-talk/test")
    check, folder = running_test(tmp_path, order, application)
    app = SimpleNamespace(
        sound_check=check,
        _quitting=False,
        voice_backfill=None,
        browser_connection=None,
        browser_watch=None,
        _record_start_generation=0,
        set_auto_recording_hold=lambda _held: None,
        _record_start_executor=SimpleNamespace(shutdown=lambda **_kwargs: None),
        speech_models=None,
        diarization_models=None,
        voice_models=None,
        library_relocator=None,
        library_recovery=None,
        _transcription_generation=0,
        _capture_background=False,
        transcriber=None,
        _transcription_holds=0,
    )
    application_class.begin_exit(app)
    assert order.events == ["test stopped"] and application.holds == 1 and folder.exists()
    exited(application)
    assert application.left == [[]] and check.closed


def test_quit_waits_for_the_discarded_test_before_quitting(application_class, tmp_path, order):  # noqa: F811
    """quit() ignores holds: finish_quit quits once the test is deleted."""
    application = Application(tmp_path / "cache/cassini-talk/test")
    check, folder = running_test(tmp_path, order, application)
    quits = []
    app = SimpleNamespace(
        sound_check=check,
        meeting_trash=SimpleNamespace(flush=lambda _callback: False),
        begin_exit=check.shutdown,
        get_windows=lambda: [],
        quit=lambda: quits.append(sorted(application.root.rglob("*"))),
    )
    app.finish_quit = types.MethodType(application_class.finish_quit, app)
    app.finish_quit()
    assert not quits and application.holds == 1
    exited(application)
    assert quits == [[]] and application.left == [[]]
    app.finish_quit()  # Nothing left: it quits at once.
    assert quits == [[], []]


def test_exit_as_the_last_window_closes_deletes_the_test(application_class, tmp_path, order):  # noqa: F811
    application = Application(tmp_path / "cache/cassini-talk/test")
    check, folder = running_test(tmp_path, order, application)
    window = SimpleNamespace(get_visible=lambda: True)
    app = fake_app(
        application_class,
        check,
        order,
        _closing_background_windows=False,
        _auto_recording_held=False,
        _capture_background_active=lambda: False,
        begin_exit=lambda: order.events.append("exit") or check.shutdown(),
    )
    app.get_windows = lambda: [window]
    application_class.window_closing(app, window)  # Discards the test, then exits.
    assert order.events == ["test stopped", "exit"]
    (recorder,) = FakeRecorder.made
    assert recorder.stopping and folder.exists() and application.holds == 1  # Held while it stops.
    exited(application)
    assert application.left == [[]] and recorder.closed


def test_no_test_after_exit_even_if_the_app_is_activated_again(application_class, tmp_path, order):  # noqa: F811
    check = SoundCheck(FakeRecorder, root=tmp_path / "cache/cassini-talk/test")
    app = fake_app(application_class, check, order)
    assert app.can_test_recording()
    check.shutdown()  # begin_exit
    app._quitting = False  # _activate_current, while a hold keeps the process alive.
    assert not app.can_test_recording()
    assert application_class.start_test_recording(app) is False
