# SPDX-License-Identifier: GPL-3.0-or-later
"""Observe GTK state while dispatching actual main-context events."""

import contextlib
from concurrent.futures import CancelledError


def dispatch_until(check, message="The GTK operation did not complete"):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    expired = False

    def expire(*_args):
        nonlocal expired
        expired = True
        return GLib.SOURCE_REMOVE

    guard = GLib.timeout_source_new_seconds(18)
    guard.set_callback(expire)
    guard.attach(context)
    try:
        while not check() and not expired:
            context.iteration(True)
        assert check(), message
    finally:
        guard.destroy()


def finish_worker(future, timeout=25):
    if future is not None:
        with contextlib.suppress(CancelledError):
            future.exception(timeout=timeout)
        assert future.done(), "A fixture writer survived teardown"


def drain_app_workers(
    app,
    *,
    windows=(),
    setup_dialogs=(),
    settings_dialogs=(),
    activity_sources=(),
    search_pages=(),
    service=None,
    service_thread=None,
):
    if app is not None:
        for setup_dialog in (*setup_dialogs, getattr(app, "companion_setup", None)):
            if setup_dialog is not None:
                if not setup_dialog.closed:
                    setup_dialog.discard()
                finish_worker(setup_dialog.future)
        for settings_dialog in (*settings_dialogs, getattr(app, "settings_dialog", None)):
            if settings_dialog is not None:
                if not settings_dialog.closed:
                    settings_dialog.force_close()
                    settings_dialog._closed()
                settings_dialog.executor.shutdown(wait=True)
                finish_worker(getattr(settings_dialog, "_future", None))
        connection = getattr(app, "browser_connection", None)
        if connection is not None:
            connection.close()
            finish_worker(connection.future)
            finish_worker(connection.health_future)
        watch = getattr(app, "browser_watch", None)
        if watch is not None:
            watch.close()
            watch.executor.shutdown(wait=True)
            finish_worker(watch.future)
        for recorder in tuple(getattr(app, "native_captures", {}).values()):
            if getattr(recorder, "_executor", None) is not None:
                recorder._executor.shutdown(wait=True)
            if getattr(recorder, "_bus_worker", None) is not None:
                recorder._bus_worker.shutdown(wait=True)
        for window in windows:
            if window is None:
                continue
            monitor = getattr(window, "monitor", None)
            if monitor is not None and getattr(monitor, "_executor", None) is not None:
                monitor._executor.shutdown(wait=True)
            activity = getattr(window, "_activity_source", None)
            if activity is not None and getattr(activity, "executor", None) is not None:
                activity.executor.shutdown(wait=True)
            search_page = getattr(window, "search_page", None)
            if search_page is not None and getattr(search_page, "_executor", None) is not None:
                search_page._executor.shutdown(wait=True)
            import_executor = getattr(window, "_import_executor", None)
            if import_executor is not None:
                import_executor.shutdown(wait=True)
        for activity in activity_sources:
            if activity is not None and getattr(activity, "executor", None) is not None:
                activity.executor.shutdown(wait=True)
        for search_page in search_pages:
            if search_page is not None and getattr(search_page, "_executor", None) is not None:
                search_page._executor.shutdown(wait=True)
        from cassini_talk import library_publication

        library_publication._ENCODER.submit(lambda: None).result(timeout=25)
        for owner_name, attr in (
            ("library_recovery", "executor"),
            ("meeting_trash", "executor"),
            ("voice_backfill", "executor"),
            ("library_relocator", "executor"),
            ("comfort", "editor"),
            ("comfort", "executor"),
            ("transcriber", "preparer"),
            ("transcriber", "executor"),
        ):
            owner = getattr(app, owner_name, None)
            executor = getattr(owner, attr, None) if owner is not None else None
            if executor is not None:
                executor.shutdown(wait=True)
        if getattr(app, "_record_start_executor", None) is not None:
            app._record_start_executor.shutdown(wait=True)
    if service is not None:
        service.closed.set()
        if service_thread is not None:
            service_thread.join(5)
            assert not service_thread.is_alive()
        service.workers.shutdown(wait=True)
