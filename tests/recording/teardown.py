# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic teardown barrier for in-process GTK recording tests.

Scripts that drive CassiniApplication via register()/activate() without app.run()
do not enter do_shutdown(), and production begin_exit()/close() calls use
shutdown(wait=False) so GTK never blocks on worker threads. Before deleting a
TemporaryDirectory Library, tests must join every worker and drain the shared
library_publication._ENCODER queue so no background encode or recovery sweep
races shutil.rmtree() on Library/.cassini.
"""

import sys
import time
from contextlib import contextmanager


def _glib():
    repository = sys.modules.get("gi.repository")
    return getattr(repository, "GLib", None) if repository is not None else None


def _pump_glib():
    glib = _glib()
    if glib is None:
        return False
    context = glib.MainContext.default()
    progressed = False
    while context.pending():
        context.iteration(False)
        progressed = True
    return progressed


def _await_capture_guard_idle(app, deadline):
    if app is None:
        return
    while getattr(app, "_capture_exiting", False) or getattr(app, "_capture_background_saving", False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for capture_guard close/background save to finish")
        glib = _glib()
        if glib is None:
            raise RuntimeError("GLib main context is required while capture_guard save is in flight")
        context = glib.MainContext.default()
        source = glib.timeout_add(max(1, int(remaining * 1000)), lambda: False)
        try:
            context.iteration(True)
        finally:
            if context.find_source_by_id(source) is not None:
                glib.source_remove(source)


def _unique(items):
    seen = set()
    ordered = []
    for item in items:
        if item is not None and id(item) not in seen:
            seen.add(id(item))
            ordered.append(item)
    return ordered


def _call_if(target, method):
    fn = getattr(target, method, None)
    if callable(fn):
        fn()


def _drain_encoder(deadline):
    try:
        from cassini_talk import library_publication
    except ImportError:
        library_publication = sys.modules.get("cassini_talk.library_publication")
    encoder = getattr(library_publication, "_ENCODER", None) if library_publication is not None else None
    if encoder is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Timed out before draining library_publication._ENCODER")
    encoder.submit(lambda: None).result(timeout=remaining)


def drain_recording_app(app, *, timeout=25):
    """Stop open UI surfaces and join every background worker before rm -rf."""
    deadline = time.monotonic() + timeout
    _pump_glib()
    if app is not None:
        _await_capture_guard_idle(app, deadline)
        windows = list(app.get_windows()) if callable(getattr(app, "get_windows", None)) else []
        recorders = _unique([getattr(app, "recorder", None), *(getattr(w, "recorder", None) for w in windows)])
        for recorder in recorders:
            _call_if(recorder, "cancel_publication")

        library_recovery = getattr(app, "library_recovery", None)
        cancelled = getattr(library_recovery, "cancelled", None)
        if cancelled is not None and callable(getattr(cancelled, "set", None)):
            cancelled.set()

        window_monitors = _unique([getattr(w, "monitor", None) for w in windows])
        search_pages = _unique([getattr(w, "search_page", None) for w in windows])
        export_dialogs = _unique([getattr(w, "export_dialog", None) for w in windows])
        activity_sources = _unique([getattr(w, "_activity_source", None) for w in windows])
        import_executors = _unique([getattr(w, "_import_executor", None) for w in windows])
        players = _unique([getattr(w, "player", None) for w in windows])

        transcriber = getattr(app, "transcriber", None)
        settings_dialog = getattr(app, "settings_dialog", None)
        companion_setup = getattr(app, "companion_setup", None)
        build_refresh = getattr(app, "build_refresh", None)
        status_notifier = getattr(app, "status_notifier", None)
        watchdog = getattr(app, "_watchdog", None)
        background_recording = getattr(app, "background_recording", None)
        global_shortcut = getattr(app, "global_shortcut", None)
        comfort = getattr(app, "comfort", None)
        sound_check = getattr(app, "sound_check", None)
        voice_backfill = getattr(app, "voice_backfill", None)
        browser_connection = getattr(app, "browser_connection", None)
        browser_watch = getattr(app, "browser_watch", None)
        browser_activity = getattr(app, "browser_activity", None)
        library_relocator = getattr(app, "library_relocator", None)
        meeting_trash = getattr(app, "meeting_trash", None)

        if windows:
            try:
                from cassini_talk.capture_guard import finish_close
            except ImportError:
                finish_close = None
            if finish_close is not None and _glib() is not None:
                finish_close(app, windows)
            else:
                for window in windows:
                    _call_if(window, "close")
                for dialog in export_dialogs:
                    if not getattr(dialog, "closed", False):
                        _call_if(dialog, "cancel")
                for player in players:
                    if hasattr(player, "shutdown"):
                        _call_if(player, "shutdown")
                    else:
                        _call_if(player, "stop")
                for source in activity_sources:
                    if not getattr(source, "closed", False):
                        _call_if(source, "close")
                for page in search_pages:
                    if not getattr(page, "closed", False):
                        _call_if(page, "close")
                for monitor in window_monitors:
                    if not getattr(monitor, "_closed", False):
                        _call_if(monitor, "close")
            _pump_glib()

        if settings_dialog is not None and not getattr(settings_dialog, "closed", False):
            _call_if(settings_dialog, "close")
        if companion_setup is not None and not getattr(companion_setup, "closed", False):
            _call_if(companion_setup, "close")
        _call_if(build_refresh, "close")
        _call_if(status_notifier, "close")
        if watchdog is not None:
            _call_if(watchdog, "close")
            app._watchdog = None
        _call_if(background_recording, "close")
        _call_if(global_shortcut, "close")
        _call_if(comfort, "close")
        _call_if(browser_activity, "close")
        _call_if(browser_connection, "close")
        _call_if(app, "begin_exit")

        for recorder in recorders:
            busy = (
                getattr(recorder, "pipeline", None) is not None
                or getattr(recorder, "starting", False)
                or getattr(recorder, "stopping", False)
                or getattr(recorder, "_finishing", False)
            )
            if not busy:
                _call_if(recorder, "close")

        producer_executors = _unique(
            [
                *(getattr(r, "_executor", None) for r in recorders),
                *(getattr(r, "_bus_worker", None) for r in recorders),
                getattr(library_recovery, "executor", None),
                getattr(library_relocator, "executor", None),
                getattr(voice_backfill, "executor", None),
                getattr(sound_check, "_executor", None),
                getattr(comfort, "editor", None),
                getattr(comfort, "executor", None),
                *(getattr(m, "_executor", None) for m in window_monitors),
                *(getattr(p, "_executor", None) for p in search_pages),
                *(getattr(d, "executor", None) for d in export_dialogs),
                *(getattr(s, "executor", None) for s in activity_sources),
                *import_executors,
                getattr(settings_dialog, "executor", None),
                getattr(browser_watch, "executor", None),
                getattr(browser_activity, "executor", None),
                getattr(app, "_record_start_executor", None),
                getattr(transcriber, "preparer", None),
                getattr(transcriber, "executor", None),
            ]
        )
        for executor in producer_executors:
            if callable(getattr(executor, "shutdown", None)):
                executor.shutdown(wait=True)

        _pump_glib()
        trash_executor = getattr(meeting_trash, "executor", None)
        if callable(getattr(trash_executor, "shutdown", None)):
            trash_executor.shutdown(wait=True)

    _drain_encoder(deadline)
    _pump_glib()


@contextmanager
def recording_app_teardown(app, *, on_error=None, stop_active=None, timeout=25):
    """Exit inner to TemporaryDirectory() so workers finish before shutil.rmtree."""
    try:
        yield
    except BaseException:
        if on_error is not None:
            on_error()
        raise
    finally:
        if stop_active is not None:
            stop_active()
        drain_recording_app(app, timeout=timeout)
