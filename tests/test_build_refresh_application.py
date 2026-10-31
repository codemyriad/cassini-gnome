# SPDX-License-Identifier: GPL-3.0-or-later
"""D42 application entry points, including stale browser activity and Open handoff."""

from types import SimpleNamespace

from test_installed_build import application, controller
from test_voice_application import application_class  # noqa: F401


def bound(app, cls, *names):
    for name in names:
        setattr(app, name, getattr(cls, name).__get__(app))


def test_open_consumes_combined_files_record_and_automatic_intent_once(application_class):  # noqa: F811
    opened, records, automatic = [], [], []
    app = SimpleNamespace(
        _resume_background=True,
        _resume_record=True,
        _resume_automatic=True,
        set_auto_recording_hold=lambda value: automatic.append(value),
        _activate_current=lambda **_kw: SimpleNamespace(open_file=opened.append),
        _record_current=lambda: records.append(True),
        build_refresh=None,
    )
    bound(app, application_class, "_take_resume_intent", "_restore_automatic")
    files = [object(), object()]
    application_class.do_open(app, files, len(files), "")
    assert opened == files and records == [True] and automatic == [True]
    assert not app._resume_record and not app._resume_background and not app._resume_automatic
    application_class.do_activate(app)
    assert records == [True] and automatic == [True], "A later Activate replayed the restart's Record intent"


def test_automatic_handoff_is_held_before_activate_or_open(application_class):  # noqa: F811
    app = SimpleNamespace(_resume_automatic=True, _auto_recording_held=False, _quitting=False)
    holds = []
    app.hold, app.release = lambda: holds.append(True), lambda: holds.pop()
    bound(app, application_class, "_restore_automatic", "set_auto_recording_hold")
    # Startup restores this before it constructs BrowserWatch or any windows.
    app._restore_automatic()
    assert app._auto_recording_held and holds == [True] and not app._resume_automatic
    app._restore_automatic()
    assert holds == [True]


def test_watch_idle_transition_checks_update_despite_stale_activity(application_class):  # noqa: F811
    app, held, finished, windows = application()
    activity = SimpleNamespace(snapshot={"one": "recording"})
    windows.append(SimpleNamespace(_activity_source=activity, get_visible=lambda: True))
    app._browser_snapshot = dict(statuses=[dict(recording=True)])
    app._auto_recording_revision = 0
    app._permission_sources = set()
    app._auto_recording_pending = None
    app._browser_seen_saved = set()
    app.browser_connection = None
    app.observe_browser_recordings = lambda *_args: None
    app.set_auto_recording_hold = lambda _: None
    app._native_changed = lambda: app.build_refresh.capture_changed()
    app.build_refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    assert not app.build_refresh.pending
    application_class._browser_changed(app, dict(statuses=[], recordings=[]))
    clock.finish()
    assert app.build_refresh.pending and not finished and not held
    activity.snapshot = {}
    # The Library's activity tick now notifies BuildRefresh when its independent
    # snapshot finally clears. It must not need a second Watch transition.
    app.build_refresh.capture_changed()
    clock.finish()
    assert finished and not held


def test_explicit_browser_stop_checks_build_even_when_no_update_known(application_class, monkeypatch):  # noqa: F811
    from test_library_monitor import spin_until

    from cassini_talk import capture_guard

    app, held, finished, _windows = application()
    app._browser_stops = set()
    app._source_changes = {}
    app._record_start_group = None
    app._notify_shortcut_saved = lambda _: None
    app.transcription_enabled = False
    app.recording_error = None
    app._native_changed = lambda: app.build_refresh.capture_changed()
    app.build_refresh, clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    monkeypatch.setattr(capture_guard, "stop_browser_captures", lambda *_args, **_kwargs: [])
    application_class.stop_browser_recordings(app)
    spin_until(lambda: not app._browser_stops)
    clock.finish()
    assert finished and not held


def test_real_restart_banner_uses_broker_busy_state(application_class):  # noqa: F811
    from cassini_talk.library_window import LibraryWindow

    app, _held, _finished, _windows = application()
    app.build_refresh, _clock = controller(app, SimpleNamespace(changed=lambda: True, restart=None))
    app.build_refresh.pending = app.build_refresh.browser_busy = True
    enabled, revealed = [], []
    app.lookup_action = lambda _: SimpleNamespace(set_enabled=enabled.append)
    window = SimpleNamespace(get_application=lambda: app, update_banner=SimpleNamespace(set_revealed=revealed.append))
    LibraryWindow.update_build_banner(window)
    assert revealed == [True] and enabled == [False]
    app.build_refresh.browser_busy = False
    LibraryWindow.update_build_banner(window)
    assert enabled == [False, True]


def test_library_activity_tick_releases_stale_capture_guard(application_class):  # noqa: F811
    from cassini_talk.library_window import LibraryWindow

    app, _held, _finished, windows = application()
    updates = []
    app.build_refresh = SimpleNamespace(capture_changed=lambda: updates.append(True))
    window = SimpleNamespace(
        get_application=lambda: app,
        _activity_source=SimpleNamespace(snapshot={}),
        _fixture_activity={},
        _activity={},
        _update_live=lambda: None,
        snapshot=None,
        meeting_page=None,
    )
    windows.append(window)
    LibraryWindow._activity_tick(window)
    assert updates == [True]
