# SPDX-License-Identifier: GPL-3.0-or-later
"""Record is a toggle: it stops only what records, never a capture that only saves."""

import logging
from types import SimpleNamespace

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


def test_try_again_and_the_shortcut_also_start_while_a_capture_only_saves(application_class):  # noqa: F811
    app, events = app_with(application_class, windows=[window({"meeting": "saving"})])
    app.retry_recording()
    assert events == ["activate", "record"]
    events.clear()
    # Windowless shortcut with Record-first skipped: starts at once, as when idle.
    app.on_global_record()
    assert events == ["start"] and not app._shortcut_saved_pending
