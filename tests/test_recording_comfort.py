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
from gi.repository import GLib  # noqa: E402

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
