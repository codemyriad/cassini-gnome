# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-owned levels/timers and browser observations without visible windows."""

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
except ValueError:
    pytest.skip("Recording observations run in the GTK-enabled Recording and distro jobs", allow_module_level=True)
from durable import durable  # noqa: E402
from gi.repository import GLib  # noqa: E402
from test_browser_capture import pending_automatic_reconnect, server  # noqa: E402, F401
from test_call_signals_capture import signals  # noqa: E402, F401

from cassini_talk.recording_comfort import RecordingComfort  # noqa: E402
from cassini_talk.recording_details import live_meeting  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402


def session(root, title, source):
    capture = RecordingSession(root, title, source=source)
    capture.add_track("mic", "Me", "microphone", 48000, 1)
    capture.append("mic", bytes(9600), 0)
    return capture


def application(recorders, *, browser=None):
    notifications = []
    return SimpleNamespace(
        native_captures=recorders,
        meeting_trash=SimpleNamespace(hides=lambda _: False),
        recorder=next(iter(recorders.values()), None),
        get_windows=lambda: [],
        get_active_window=lambda: None,
        _browser_snapshot=browser or {},
        browser_capture_active=lambda: bool(browser),
        inhibit=lambda *_: 42,
        uninhibit=lambda *_: None,
        send_notification=lambda identifier, _notification: notifications.append(identifier),
        withdraw_notification=lambda *_: None,
        notifications=notifications,
        background_recording=SimpleNamespace(status=lambda *_: None),
        windowless_background=SimpleNamespace(refresh=lambda *_: None),
    )


def close(comfort):
    comfort.close()
    comfort.executor.shutdown(wait=True, cancel_futures=True)
    comfort.editor.shutdown(wait=True)


def test_each_native_session_keeps_its_levels_and_clock_when_another_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    captures = [session(tmp_path, title, "gnome") for title in ("Earlier", "Later")]
    natives = [
        SimpleNamespace(
            session=capture,
            pipeline=object(),
            starting=False,
            stopping=False,
            _finishing=False,
            levels=levels,
            minutes_left=50,
            line=None,
            _start_us=GLib.get_monotonic_time() - elapsed * 1000,
        )
        for capture, levels, elapsed in zip(captures, ([0.2, 0.4], [0.6, 0.8]), (125500, 65500), strict=True)
    ]
    app = application(dict(enumerate(natives)))
    comfort = RecordingComfort(app, automatic=False)
    meetings = [
        replace(live_meeting(capture.directory), started_at_ms=index * 1000)
        for index, capture in enumerate(captures, start=1)
    ]
    comfort.known = {meeting.key: meeting for meeting in meetings}
    try:
        comfort.native_level()
        comfort.tick()
        assert len(comfort.active()) == 2
        assert comfort.status(meetings[0])["levels"] == [0.2, 0.4]
        assert comfort.status(meetings[1])["levels"] == [0.6, 0.8]
        assert comfort.elapsed(meetings[0]) >= 125500
        assert 65500 <= comfort.elapsed(meetings[1]) < 70000
        assert comfort.last_payload["title"] == "Cassini is recording"
        assert "Earlier" in comfort.last_payload["body"]
        natives[0].pipeline = None
        captures[0].finish()
        comfort.tick()
        assert set(comfort.active()) == {meetings[1].key}
        assert comfort.status(meetings[1])["levels"] == [0.6, 0.8]
        assert "Later" in comfort.last_payload["body"]
    finally:
        close(comfort)
        for capture in captures:
            if not capture.finished:
                capture.finish()


def test_application_browser_inventory_drives_hidden_notifications_without_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    capture = session(tmp_path, "Hidden browser call", "chromium")
    browser = dict(statuses=[dict(recording=True, path=str(capture.directory))])
    app = application({}, browser=browser)
    comfort = RecordingComfort(app, automatic=False)
    try:
        comfort.active()
        end = time.monotonic() + 2
        while capture.directory.name not in comfort.known and time.monotonic() < end:
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)
        assert capture.directory.name in comfort.active()
        comfort.tick()
        assert "Hidden browser call" in comfort.last_payload["body"]
        assert "recording" in app.notifications and comfort.browser_cookie == 42
    finally:
        close(comfort)
        capture.finish()


@pytest.mark.parametrize("state", ["saved", "recording", "missing"])
def test_saved_notification_waits_for_readable_finished_audio_and_releases_its_hold(tmp_path, monkeypatch, state):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    capture = session(tmp_path, "Final title", "gnome")
    if state == "saved":
        capture.finish()
    holds, notifications = [], []
    app = SimpleNamespace(
        _quitting=False,
        hold=lambda: holds.append("hold"),
        release=lambda: holds.append("release"),
        send_notification=lambda identifier, _: notifications.append(identifier),
    )
    comfort = RecordingComfort(app, automatic=False)
    try:
        comfort.saved_notification(capture.directory if state != "missing" else tmp_path / "Recording-missing")
        end = time.monotonic() + 3
        while holds != ["hold", "release"] and time.monotonic() < end:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.005)
        assert holds == ["hold", "release"]
        assert notifications == (["saved-" + capture.directory.name] if state == "saved" else [])
    finally:
        close(comfort)
        if not capture.finished:
            capture.finish()


def test_stopping_browser_capture_stays_active_while_publication_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    capture = session(tmp_path, "Saving browser call", "chromium")
    meeting = live_meeting(capture.directory, "recording")
    app = application({}, browser=dict(statuses=[dict(recording=False, stopping=True, path=str(capture.directory))]))
    comfort = RecordingComfort(app, automatic=False)
    comfort.known[meeting.key] = meeting
    try:
        assert set(comfort.active()) == {meeting.key}
        assert comfort.active()[meeting.key].lifecycle == "saving"
    finally:
        close(comfort)
        capture.finish()


@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
def test_committed_browser_loss_alerts_once_and_saved_details_keep_the_cause(tmp_path, monkeypatch, visible, automatic):
    from test_browser_capture import Tap

    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.capture_details import read_details
    from cassini_talk.strings import text

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Tap)
    now = [1000]
    capture.elapsed_ms = lambda: now[0]
    capture.start(dict(type="start", captureId="alert", timelineOriginMs=1, microphone=True))
    capture.handle(dict(type="track", id="voice", kind="remote", sampleRate=48000, channels=1))
    capture.handle(dict(type="audio", id="voice", sequence=0, offsetMs=0, pcm="AQACAA=="))
    status = dict(recording=True, automatic=automatic, path=str(capture.directory))
    app = application({}, browser=dict(statuses=[status]))
    app.get_windows = lambda: [SimpleNamespace(get_visible=lambda: visible)]
    comfort = RecordingComfort(app, automatic=False)
    alert = "recording-fallback-" + capture.directory.name

    def observe():
        current = live_meeting(capture.directory)
        comfort.known[current.key] = current
        comfort.tick()
        return current

    try:
        observe()
        assert alert not in app.notifications
        # The actual producer Stop commits a live fallback, rather than ending
        # the recording. The notification reads that checkpoint from disk.
        capture.handle(dict(type="stop", failureReason="lost", cause="frameError", captureId="alert"))
        identifier, tap = capture.native_taps["system"]
        track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
        tap.append(bytes(9600), track["startOffsetMs"])
        meeting = observe()
        assert comfort.sources(meeting).fallback
        assert not capture.finished
        assert app.notifications.count(alert) == int(automatic)
        observe()
        comfort._fallback_notification(meeting)
        assert app.notifications.count(alert) == int(automatic)
        # A restored capture can lose the browser again: alert once for each
        # committed event rather than only once during the whole recording.
        now[0] = 1200
        capture.handle(dict(type="audio", id="voice", sequence=1, offsetMs=1200, pcm="AQACAA=="))
        capture.restore()
        assert not comfort.sources(observe()).fallback
        now[0] = 1300
        capture.handle(dict(type="stop", failureReason="lost", cause="frameClosed", captureId="alert"))
        identifier, tap = capture.native_taps["system"]
        track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
        tap.append(bytes(9600), track["startOffsetMs"])
        meeting = observe()
        assert app.notifications.count(alert) == 2 * int(automatic)
        # Saved Details already has the cause-specific copy; normal Stop does
        # not produce another loss notification.
        now[0] = 1500
        capture.handle(dict(type="stop", reason="user", captureId="alert"))
        saved = live_meeting(capture.directory, "saved")
        status["recording"] = False
        comfort._fallback_notification(saved)
        assert app.notifications.count(alert) == 2 * int(automatic)
        assert text("details.othersFallback.cause.frameError") in read_details(saved).plain_text()
        assert text("details.othersFallback.cause.frameClosed") in read_details(saved).plain_text()
    finally:
        close(comfort)
        capture.stop()


@pytest.mark.parametrize("source,reason", [("browser", "start"), ("system", "missing"), ("system", "user")])
def test_automatic_capture_without_committed_loss_never_alerts(tmp_path, monkeypatch, source, reason):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    capture = session(tmp_path, "No browser loss", "chromium")
    with capture.change([dict(type="othersSource", source=source, reason=reason, atMs=0)]):
        pass
    app = application({}, browser=dict(statuses=[dict(recording=True, automatic=True, path=str(capture.directory))]))
    comfort = RecordingComfort(app, automatic=False)
    try:
        comfort._fallback_notification(live_meeting(capture.directory))
        capture.finish(reason="user")
        comfort._fallback_notification(live_meeting(capture.directory, "saved"))
        assert app.notifications == []
    finally:
        close(comfort)
        if not capture.finished:
            capture.finish()


@pytest.mark.parametrize("commit", [False, True])
def test_loss_alert_waits_for_checkpoint_and_failed_change_stays_quiet(tmp_path, monkeypatch, commit):
    from contextlib import nullcontext

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    capture = session(tmp_path, "Checkpointed loss", "chromium")
    with capture.change([dict(type="selection", me=True, others=True, reason="start", atMs=0)]):
        pass
    app = application({}, browser=dict(statuses=[dict(recording=True, automatic=True, path=str(capture.directory))]))
    comfort = RecordingComfort(app, automatic=False)
    try:
        with (
            nullcontext() if commit else pytest.raises(OSError, match="checkpoint refused"),
            capture.change([dict(type="othersSource", source="system", reason="lost", atMs=100)]),
        ):
            # The proposed in-memory event is not the on-disk checkpoint.
            comfort._fallback_notification(live_meeting(capture.directory))
            assert app.notifications == []
            if not commit:
                raise OSError("checkpoint refused")
        comfort._fallback_notification(live_meeting(capture.directory))
        assert len(app.notifications) == int(commit)
        if commit:
            with capture.change([dict(type="othersSource", source="browser", reason="restored", atMs=100)]):
                pass
            with capture.change([dict(type="othersSource", source="system", reason="lost", atMs=100)]):
                pass
            comfort._fallback_notification(live_meeting(capture.directory))
            assert len(app.notifications) == 2, "Equal event timestamps must not collapse two losses"
    finally:
        close(comfort)
        capture.finish()


def test_recovered_automatic_start_still_alerts_when_browser_audio_is_lost(
    pending_automatic_reconnect,  # noqa: F811
    monkeypatch,
):
    case = pending_automatic_reconnect
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(case.capture.directory.parent))
    case.release.set()
    durable(case.completed, "automatic Start recovery did not finish")
    assert not case.errors
    case.client.handle(dict(type="stop", captureId="recovered-origin", failureReason="lost", cause="frameClosed"))
    assert case.capture.browser_lost and not case.capture.finished
    app = application({}, browser=case.service.snapshot())
    comfort = RecordingComfort(app, automatic=False)
    try:
        meeting = live_meeting(case.capture.directory)
        comfort._fallback_notification(meeting)
        comfort._fallback_notification(meeting)
        assert app.notifications == ["recording-fallback-" + meeting.key]
    finally:
        close(comfort)


def test_signal_owned_native_broker_capture_is_visible_without_browser_loss_alert(
    signals,  # noqa: F811
):
    from test_call_signals import call

    from cassini_talk.browser_service import request

    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    capture = d.started()
    app = application({}, browser=d.service.snapshot())
    comfort = RecordingComfort(app, automatic=False)
    meeting = live_meeting(capture.directory)
    comfort.known[meeting.key] = meeting
    try:
        assert meeting.capture_source == "gnome"
        assert meeting.key in comfort.active()
        assert comfort.sources(meeting).source == "system" and not comfort.sources(meeting).fallback
        comfort._fallback_notification(meeting)
        assert not app.notifications
    finally:
        close(comfort)


def test_pending_signal_start_is_visible_to_capture_controls_before_its_folder_exists(
    signals,  # noqa: F811
    monkeypatch,
):
    import threading

    from test_call_signals import call

    from cassini_talk.browser_service import request
    from cassini_talk.capture_guard import capture_targets

    d = signals
    entered, release, finished = (threading.Event() for _ in range(3))
    original = d.service._start_signal

    def held(*args):
        entered.set()
        durable(release, "test did not release pending input startup")
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(d.service, "_start_signal", held)
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    try:
        d.report(p, [call()])
        durable(entered, "signal Start did not reserve its owner")
        app = application({}, browser=d.service.snapshot())
        assert capture_targets(app) == ([], True)
        request("stop-all", path=d.path)
        capture = next(iter(d.service.captures.values()))
        assert capture.stop_requested.is_set()
        release.set()
        durable(finished, "globally stopped signal Start did not finish")
        assert not capture.native_taps and capture.directory is None
    finally:
        release.set()


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("failed_first", [False, True])
def test_refresh_during_old_observation_is_coalesced_and_retirement_cancels_it(
    tmp_path, monkeypatch, retired, failed_first
):
    from concurrent.futures import Future

    from test_browser_capture import Tap

    from cassini_talk.browser_capture import BrowserCapture

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Tap)
    capture.elapsed_ms = lambda: 1000
    capture.start(dict(type="start", captureId="coalesced", timelineOriginMs=1, microphone=True))
    capture.handle(dict(type="track", id="voice", kind="remote", sampleRate=48000, channels=1))
    capture.handle(dict(type="audio", id="voice", sequence=0, offsetMs=0, pcm="AQACAA=="))
    before = live_meeting(capture.directory)
    status = dict(recording=True, automatic=True, path=str(capture.directory))
    app = application({}, browser=dict(statuses=[status]))
    comfort = RecordingComfort(app, automatic=False)
    comfort.known[before.key] = before
    callbacks, jobs = [], []

    class ObserverQueue:
        def submit(self, _function, *_args):
            future = Future()
            jobs.append(future)
            return future

        def shutdown(self, **_kwargs):
            for future in jobs:
                future.cancel()

    def deliver():
        while callbacks:
            function, args = callbacks.pop(0)
            function(*args)

    monkeypatch.setattr(GLib, "idle_add", lambda function, *args: callbacks.append((function, args)) or 1)
    comfort.executor.shutdown(wait=True)
    comfort.executor = ObserverQueue()
    values = dict(
        levels=[0.0, 0.0],
        voices=1,
        minutes=50,
        reserve=False,
        recent_sound=False,
        output_muted=False,
        source_problem=False,
    )
    alert = "recording-fallback-" + before.key
    try:
        comfort.tick()  # This read captured the state before fallback.
        assert len(jobs) == 1
        capture.handle(dict(type="stop", failureReason="lost", cause="frameError", captureId="coalesced"))
        after = live_meeting(capture.directory)
        assert after.capture_events != before.capture_events
        comfort.tick()  # Must retain this newer observation request.
        comfort.tick()  # Several requests still mean only one follow-up.
        if retired:
            app._browser_snapshot = dict(statuses=[])
        if failed_first:
            jobs[0].set_exception(OSError("older read failed"))
        else:
            jobs[0].set_result((before, values))
        deliver()
        assert len(jobs) == (1 if retired else 2)
        if not retired:
            assert alert not in app.notifications
            jobs[1].set_result((after, values))
            deliver()
            assert comfort.sources(before).fallback
            assert app.notifications.count(alert) == 1
            assert not comfort.futures
            assert not comfort._observe_again
        else:
            assert before.key not in comfort.active()
            assert not comfort._observe_again
            assert alert not in app.notifications
    finally:
        close(comfort)
        capture.stop()
