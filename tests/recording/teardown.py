# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic teardown barrier for in-process GTK recording tests.

Scripts that drive CassiniApplication via register()/activate() without app.run()
do not enter do_shutdown(), and production begin_exit()/close() calls use
shutdown(wait=False) so GTK never blocks on worker threads. Before deleting a
test Library directory, tests must join every worker within a bounded deadline
and retain the directory if any writer or active capture cannot be stopped.
"""

import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import CancelledError
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FunctionType


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


def _await_capture_guard_idle(app, deadline, clock, outstanding):
    if app is None:
        return
    while getattr(app, "_capture_exiting", False) or getattr(app, "_capture_background_saving", False):
        remaining = deadline - clock()
        if remaining <= 0:
            outstanding.append("app.capture_guard")
            return
        glib = _glib()
        if glib is None:
            outstanding.append("app.capture_guard[no-glib-context]")
            return
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


def _unique_named(pairs):
    seen = set()
    ordered = []
    for label, item in pairs:
        if item is not None and id(item) not in seen:
            seen.add(id(item))
            ordered.append((label, item))
    return ordered


def _call_if(target, method):
    fn = getattr(target, method, None)
    if callable(fn):
        fn()


def _shutdown_now(executor):
    shutdown = getattr(executor, "shutdown", None)
    if not callable(shutdown):
        return
    try:
        shutdown(wait=False, cancel_futures=True)
    except TypeError:
        shutdown(wait=False)


def _join_executor(label, executor, deadline, clock, outstanding):
    threads = getattr(executor, "_threads", None)
    if threads is not None:
        joined = set()
        while True:
            pending = [
                thread
                for thread in tuple(threads)
                if thread not in joined and thread is not threading.current_thread() and thread.is_alive()
            ]
            if not pending:
                return
            for thread in pending:
                joined.add(thread)
                remaining = max(0.0, deadline - clock())
                thread.join(timeout=remaining)
                if thread.is_alive():
                    outstanding.append(f"{label}[{thread.name}]")
            if any(thread.is_alive() for thread in pending):
                return
        return

    shutdown = getattr(executor, "shutdown", None)
    if callable(shutdown):
        done = threading.Event()

        def _wait():
            try:
                shutdown(wait=True)
            finally:
                done.set()

        waiter = threading.Thread(target=_wait, name=f"drain-{label}", daemon=True)
        waiter.start()
        if not done.wait(timeout=max(0.0, deadline - clock())):
            outstanding.append(label)


def _join_future(label, future, deadline, clock, outstanding):
    if future is None or not callable(getattr(future, "done", None)) or future.done():
        return
    remaining = max(0.0, deadline - clock())
    with suppress(CancelledError, FuturesTimeoutError, TimeoutError):
        future.exception(timeout=remaining)
    if not future.done():
        outstanding.append(label)


def _publication_encoder():
    try:
        from cassini_talk import library_publication
    except ImportError:
        library_publication = sys.modules.get("cassini_talk.library_publication")
    return getattr(library_publication, "_ENCODER", None) if library_publication is not None else None


def _encoder_appears_idle(encoder):
    if encoder is None:
        return True
    threads = getattr(encoder, "_threads", None)
    queue = getattr(encoder, "_work_queue", None)
    return threads is not None and len(threads) == 0 and queue is not None and queue.empty()


def _record_encoder_timeout(encoder, probe, outstanding):
    probe.cancel()
    threads = [t.name for t in tuple(getattr(encoder, "_threads", ())) if t.is_alive()]
    detail = f"[{', '.join(threads)}]" if threads else ""
    outstanding.append(f"library_publication._ENCODER{detail}")


def drain_recording_app(app, *, timeout=25, clock=time.monotonic):
    """Stop open UI surfaces and join every background worker within `timeout`."""
    deadline = clock() + timeout
    outstanding = []
    _pump_glib()

    encoder = _publication_encoder()
    initial_probe = None if _encoder_appears_idle(encoder) else encoder.submit(lambda: None)

    if app is not None:
        _await_capture_guard_idle(app, deadline, clock, outstanding)
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
            else:
                outstanding.append(
                    "recorder("
                    f"pipeline={getattr(recorder, 'pipeline', None) is not None}, "
                    f"starting={bool(getattr(recorder, 'starting', False))}, "
                    f"stopping={bool(getattr(recorder, 'stopping', False))}, "
                    f"finishing={bool(getattr(recorder, '_finishing', False))})"
                )

        producer_executors = _unique_named(
            [
                *((f"recorder[{i}]._executor", getattr(r, "_executor", None)) for i, r in enumerate(recorders)),
                *((f"recorder[{i}]._bus_worker", getattr(r, "_bus_worker", None)) for i, r in enumerate(recorders)),
                ("library_recovery.executor", getattr(library_recovery, "executor", None)),
                ("library_relocator.executor", getattr(library_relocator, "executor", None)),
                ("voice_backfill.executor", getattr(voice_backfill, "executor", None)),
                ("sound_check._executor", getattr(sound_check, "_executor", None)),
                ("comfort.editor", getattr(comfort, "editor", None)),
                ("comfort.executor", getattr(comfort, "executor", None)),
                *(
                    (f"window.monitor[{i}]._executor", getattr(m, "_executor", None))
                    for i, m in enumerate(window_monitors)
                ),
                *((f"search_page[{i}]._executor", getattr(p, "_executor", None)) for i, p in enumerate(search_pages)),
                *((f"export_dialog[{i}].executor", getattr(d, "executor", None)) for i, d in enumerate(export_dialogs)),
                *(
                    (f"activity_source[{i}].executor", getattr(s, "executor", None))
                    for i, s in enumerate(activity_sources)
                ),
                *((f"window[{i}]._import_executor", imp) for i, imp in enumerate(import_executors)),
                ("settings_dialog.executor", getattr(settings_dialog, "executor", None)),
                ("browser_watch.executor", getattr(browser_watch, "executor", None)),
                ("browser_activity.executor", getattr(browser_activity, "executor", None)),
                ("app._record_start_executor", getattr(app, "_record_start_executor", None)),
                ("transcriber.preparer", getattr(transcriber, "preparer", None)),
                ("transcriber.executor", getattr(transcriber, "executor", None)),
            ]
        )
        for _label, executor in producer_executors:
            _shutdown_now(executor)
        for label, executor in producer_executors:
            _join_executor(label, executor, deadline, clock, outstanding)

        for label, future in (
            ("companion_setup.future", getattr(companion_setup, "future", None)),
            ("settings_dialog._future", getattr(settings_dialog, "_future", None)),
            ("browser_connection.future", getattr(browser_connection, "future", None)),
            ("browser_connection.health_future", getattr(browser_connection, "health_future", None)),
            ("browser_watch.future", getattr(browser_watch, "future", None)),
        ):
            _join_future(label, future, deadline, clock, outstanding)

        _pump_glib()
        trash_executor = getattr(meeting_trash, "executor", None)
        if trash_executor is not None:
            _shutdown_now(trash_executor)
            _join_executor("meeting_trash.executor", trash_executor, deadline, clock, outstanding)

    encoder_timed_out = False
    if initial_probe is not None and not initial_probe.done():
        try:
            initial_probe.result(timeout=max(0.0, deadline - clock()))
        except FuturesTimeoutError:
            _record_encoder_timeout(encoder, initial_probe, outstanding)
            encoder_timed_out = True

    if not encoder_timed_out and not outstanding and not _encoder_appears_idle(encoder):
        final_probe = encoder.submit(lambda: None)
        if not final_probe.done():
            try:
                final_probe.result(timeout=max(0.0, deadline - clock()))
            except FuturesTimeoutError:
                _record_encoder_timeout(encoder, final_probe, outstanding)

    _pump_glib()
    if outstanding:
        raise TimeoutError(
            f"Recording teardown exceeded {timeout}s budget; outstanding workers: {', '.join(outstanding)}"
        )


def _resolve_app(app):
    return app() if isinstance(app, FunctionType) else app


@contextmanager
def recording_app_teardown(
    app,
    *,
    directory=None,
    prefix="cassini-recording-",
    on_error=None,
    stop_active=None,
    after_drain_on_error=None,
    before_cleanup=None,
    timeout=25,
    clock=time.monotonic,
):
    """Manage a test Library directory so rmtree never runs while writers remain active."""
    target_dir = Path(directory) if directory is not None else Path(tempfile.mkdtemp(prefix=prefix))
    body_error = None
    hook_errors = []
    stop_error = None
    drain_error = None
    try:
        yield str(target_dir)
    except BaseException as exc:
        body_error = exc
        if on_error is not None:
            try:
                on_error()
            except BaseException as hook_exc:
                hook_errors.append(hook_exc)
    finally:
        if stop_active is not None:
            try:
                stop_active()
            except BaseException as stop_exc:
                stop_error = stop_exc
        try:
            drain_recording_app(_resolve_app(app), timeout=timeout, clock=clock)
        except BaseException as drain_exc:
            drain_error = drain_exc
        failed = body_error is not None or stop_error is not None or drain_error is not None or bool(hook_errors)
        if failed and after_drain_on_error is not None:
            try:
                after_drain_on_error(target_dir)
            except BaseException as after_exc:
                hook_errors.append(after_exc)
        if before_cleanup is not None:
            try:
                before_cleanup()
            except BaseException as cleanup_exc:
                hook_errors.append(cleanup_exc)
        writers_stopped = stop_error is None and drain_error is None
        if writers_stopped:
            shutil.rmtree(target_dir)
        else:
            note = f"Retained recording test directory because writers remained active or stop failed: {target_dir}"
            for err in (drain_error, stop_error, body_error, *hook_errors):
                if err is not None and hasattr(err, "add_note"):
                    err.add_note(note)
                    break
        errors = [err for err in (body_error, *hook_errors, stop_error, drain_error) if err is not None]
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            if all(isinstance(err, Exception) for err in errors):
                raise ExceptionGroup("Recording scenario and teardown failed", errors) from None
            raise BaseExceptionGroup("Recording scenario and teardown failed", errors) from None
