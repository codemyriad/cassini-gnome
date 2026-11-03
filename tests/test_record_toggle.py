# SPDX-License-Identifier: GPL-3.0-or-later
"""Record is a toggle: it stops only what records, never a capture that only saves."""

import logging
import queue
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_voice_application import application_class  # noqa: F401


def window(activity=None, statuses=()):
    return SimpleNamespace(
        _activity_source=SimpleNamespace(snapshot=dict(activity or {}), connected=False),
        _browser_snapshot=dict(statuses=list(statuses)),
        get_visible=lambda: False,
        is_active=lambda: False,
    )


def app_with(application_class, windows=(), statuses=(), native=(), starting=None):  # noqa: F811
    events = []
    app = SimpleNamespace(
        _record_start_future=starting,
        _browser_snapshot=dict(statuses=list(statuses)),
        native_captures={i: recorder for i, recorder in enumerate(native)},
        get_windows=lambda: list(windows),
        build_refresh=None,
        recording_failure=None,
        record_first=None,
        _shortcut_saved_pending=set(),
        recording_settings=SimpleNamespace(get_boolean=lambda key: key == "skip-record-first"),
        _activate_current=lambda: events.append("activate") or object(),
        _record_current=lambda: events.append("record"),
        start_recordings=lambda: events.append("start"),
        on_stop_recording=lambda *_: events.append("stop"),
        _native_changed=lambda *_: None,
    )
    for name in ("recording_to_stop", "on_record", "retry_recording", "on_global_record"):
        setattr(app, name, getattr(application_class, name).__get__(app))
    return app, events


def idle_recorder():
    return SimpleNamespace(pipeline=None, starting=False, stopping=False, _finishing=False)


def test_record_while_the_previous_browser_capture_only_saves_starts_a_recording(application_class):  # noqa: F811
    # The Library's 1 s BrowserActivity poll still lists the stopped capture as
    # saving while the top bar already reads Ready: Record once became a silent
    # Stop here (Desktop indicator.py "second recording").
    app, events = app_with(application_class, windows=[window({"meeting": "saving"})])
    app.on_record()
    assert events == ["activate", "record"]


def test_a_stopping_browser_status_alone_does_not_turn_record_into_stop(application_class):  # noqa: F811
    stopping = dict(recording=False, stopping=True, path="/tmp/Recording-x")
    app, events = app_with(application_class, statuses=[stopping], windows=[window(statuses=[stopping])])
    app.on_record()
    assert events == ["activate", "record"]


@pytest.mark.parametrize(
    "where, reason",
    [
        ("activity", "browser activity"),
        ("app-status", "browser"),
        ("window-status", "browser"),
        ("native", "native"),
        ("starting", "start"),
    ],
)
def test_record_stops_what_records_and_names_it(application_class, caplog, where, reason):  # noqa: F811
    live = dict(recording=True, path="/tmp/Recording-x")
    busy = SimpleNamespace(pipeline=object(), starting=False, stopping=False, _finishing=False)
    app, events = app_with(
        application_class,
        windows=[
            window({"meeting": "recording"} if where == "activity" else {}, [live] if where == "window-status" else [])
        ],
        statuses=[live] if where == "app-status" else [],
        native=[busy if where == "native" else idle_recorder()],
        starting=object() if where == "starting" else None,
    )
    with caplog.at_level(logging.INFO, logger="cassini.lifecycle"):
        app.on_record()
    assert events == ["stop"]
    assert f"Record stops the current recording ({reason})" in caplog.text


def pump(condition, label, bound_ms=5000):
    """Run the main loop until the event arrives; the bound only fails a hang."""
    from gi.repository import GLib

    expired = []
    source = GLib.timeout_add(bound_ms, lambda: expired.append(True) or False)
    try:
        while not condition():
            assert not expired, label
            GLib.MainContext.default().iteration(True)
    finally:
        if not expired:
            GLib.source_remove(source)


def test_the_brokers_stopping_status_wins_over_a_held_activity_poll(application_class, monkeypatch):  # noqa: F811
    from cassini_talk import capture_activity

    identifier = str(uuid4())
    path = "/library/Recording-" + identifier
    replies = queue.Queue()
    # The real BrowserActivity; each poll answers only when the test says so.
    monkeypatch.setattr(capture_activity, "read_browser_activity", lambda *_: replies.get())
    changes = []
    activity = capture_activity.BrowserActivity(lambda: changes.append(1), interval_ms=3_600_000)
    try:
        replies.put(({identifier: "recording"}, False, {identifier: path}))
        pump(lambda: changes, "first activity poll")
        library = window()
        library._activity_source = activity
        app, events = app_with(application_class, windows=[library])
        app._browser_snapshot = dict(statuses=[dict(recording=True, path=path)])
        app.on_record()
        assert events == ["stop"]  # It records: Record is Stop.

        # The next poll is held; the broker's Watch already reports the stop.
        activity.poll()
        app._browser_snapshot = dict(statuses=[dict(recording=False, stopping=True, path=path)])
        assert activity.snapshot == {identifier: "recording"}  # Stale for up to 1 s.
        events.clear()
        app.on_record()
        assert events == ["activate", "record"]  # The new recording starts...

        replies.put(({identifier: "saving"}, False, {identifier: path}))
        pump(lambda: activity.snapshot == {identifier: "saving"}, "held activity poll")
        events.clear()
        app.on_record()
        assert events == ["activate", "record"]  # ...while the old one saves.
    finally:
        replies.put(({}, False, {}))  # Never leave a poll thread blocked on a failure.
        activity.close()


def test_an_independent_live_capture_still_makes_record_stop(application_class):  # noqa: F811
    saving, live = str(uuid4()), str(uuid4())
    stopping = dict(recording=False, stopping=True, path="/library/Recording-" + saving)
    app, events = app_with(
        application_class,
        windows=[window({saving: "recording", live: "recording"})],
        statuses=[stopping],
    )
    app.on_record()
    assert events == ["stop"]  # The other meeting still records.


def test_try_again_and_the_shortcut_also_start_while_a_capture_only_saves(application_class):  # noqa: F811
    app, events = app_with(application_class, windows=[window({"meeting": "saving"})])
    app.retry_recording()
    assert events == ["activate", "record"]
    events.clear()
    # Windowless shortcut with Record-first skipped: starts at once, as when idle.
    app.on_global_record()
    assert events == ["start"] and not app._shortcut_saved_pending
