# SPDX-License-Identifier: GPL-3.0-or-later
"""D60 (R83): the test recording's judge, lifetime and private folder.

A scripted recorder stands in for the devices; every wait is on a GLib
event (a signal or an idle callback), with a timeout only as a bound.
"""

import os
import stat
import types
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
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
        output.writeframes(np.clip(np.round(samples * 32767), -32768, 32767).astype("<i2").tobytes())
    return len(samples)


def tone(dbfs, seconds=2.0, frequency=440):
    t = np.arange(int(seconds * RATE)) / RATE
    return 10 ** (dbfs / 20) * np.sqrt(2) * np.sin(2 * np.pi * frequency * t)  # RMS at dbfs


def noise(dbfs, seconds=2.0):
    return 10 ** (dbfs / 20) * np.random.default_rng(7).standard_normal(int(seconds * RATE))


# The judge: D52.4/G10's activity floor (-50 dBFS alone below 10 s of audio).


def test_a_tone_is_working(tmp_path):
    write_wav(tmp_path / "a.wav", tone(-20))
    assert judge(tmp_path / "a.wav") == WORKING


def test_a_quiet_tone_above_the_floor_is_working(tmp_path):
    write_wav(tmp_path / "a.wav", tone(-40))
    assert judge(tmp_path / "a.wav") == WORKING


def test_digital_silence_is_no_sound_heard(tmp_path):
    write_wav(tmp_path / "a.wav", np.zeros(2 * RATE))
    assert judge(tmp_path / "a.wav") == SILENT


def test_noise_below_the_floor_is_no_sound_heard(tmp_path):
    write_wav(tmp_path / "a.wav", noise(-62))
    assert judge(tmp_path / "a.wav") == SILENT


def test_a_click_shorter_than_an_active_run_is_no_sound_heard(tmp_path):
    samples = np.zeros(2 * RATE)
    samples[RATE : RATE + RATE // 20] = tone(-10, 0.05)  # 50 ms: under the 160 ms run
    write_wav(tmp_path / "a.wav", samples)
    assert judge(tmp_path / "a.wav") == SILENT


def test_each_stream_is_judged_on_its_own_track(tmp_path):
    write_wav(tmp_path / "track-microphone.wav", tone(-20))
    write_wav(tmp_path / "track-system.wav", np.zeros(RATE))
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


@pytest.fixture
def check(tmp_path):
    FakeRecorder.made.clear()
    check = SoundCheck(FakeRecorder, root=tmp_path / "cache/cassini-talk/test", duration_ms=20)
    check.signals = []
    check.connect("changed", lambda c: c.signals.append(("changed", c.state)))
    check.connect("finished", lambda c: c.signals.append(("finished", c.state)))
    yield check
    check.shutdown()


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
    recorder.open(microphone=tone(-20), system=np.zeros(RATE))
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


def test_shutdown_deletes_at_once(check):
    recorder = started(check)
    check.duration_ms = 60_000
    recorder.open(microphone=tone(-20), system=tone(-20))
    folder = recorder.parent
    check.shutdown()
    check._executor.shutdown(wait=True)
    assert not folder.exists() and recorder.stops == 1
    spin_until(lambda: recorder.closed)
    with pytest.raises(RuntimeError):
        check.start()


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


def running_test(tmp_path, order):
    check = SoundCheck(FakeRecorder, root=tmp_path / "cache/cassini-talk/test", duration_ms=60_000)
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
        _activate_current=lambda: None,
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


def test_exiting_deletes_a_running_test_at_once(application_class, tmp_path, order):  # noqa: F811
    check, folder = running_test(tmp_path, order)
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
    check._executor.shutdown(wait=True)  # The deletion was queued before the exit.
    assert not folder.exists() and order.events == ["test stopped"]
