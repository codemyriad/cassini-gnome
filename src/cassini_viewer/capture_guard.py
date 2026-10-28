# SPDX-License-Identifier: GPL-3.0-or-later
"""UX2 confirmation 1: explicit stop-and-keep or background recording."""

from concurrent.futures import ThreadPoolExecutor

from .strings import text


def recorder_busy(recorder):
    return recorder is not None and bool(
        recorder.pipeline is not None
        or getattr(recorder, "starting", False)
        or getattr(recorder, "stopping", False)
        or getattr(recorder, "_finishing", False)
    )


def capture_targets(app):
    native = [w for w in app.get_windows() if recorder_busy(getattr(w, "recorder", None))]
    if recorder_busy(getattr(app, "recorder", None)):
        native.append(app)
    browser = any(getattr(w, "_activity_source", None) and w._activity_source.snapshot for w in app.get_windows())
    browser = browser or any(
        any(s.get("recording") for s in getattr(w, "_browser_snapshot", {}).get("statuses", []))
        for w in app.get_windows()
    )
    return native, bool(browser)


def refresh_capture_background(app):
    """Own the background hold only while a hidden capture actually needs it."""
    if not getattr(app, "_capture_background", False):
        return False
    windows = app.get_windows()
    native, browser = capture_targets(app)
    if not any(w.get_visible() for w in windows) and (native or browser):
        return True
    app._capture_background = False
    app.release()
    if not native and not browser and windows and not any(w.get_visible() for w in windows):
        finish_close(app, windows)
    return False


def confirm_capture_close(app, window, *, quitting=False):
    from gi.repository import Adw, GLib

    if getattr(app, "_capture_close_allowed", False):
        return False
    # The guard covers the dialog AND all asynchronous finalization, including
    # the interval after pipelines disappear but before metadata is saved.
    if getattr(app, "_capture_confirmation", None):
        return True
    native, browser = capture_targets(app)
    if not native and not browser:
        return False
    dialog = Adw.AlertDialog(heading=text("confirm.recordingRunning.title"), body=text("confirm.recordingRunning.body"))
    dialog.add_response("keep", text("confirm.recordingRunning.secondary"))
    dialog.add_response("stop", text("confirm.recordingRunning.primary"))
    dialog.set_default_response("stop")
    dialog.set_close_response("keep")
    app._capture_confirmation = dialog
    responded = False

    def response(_dialog, choice):
        nonlocal responded
        if responded:
            return
        responded = True
        targets = list(app.get_windows()) if quitting else [window]
        if choice == "keep":
            app._capture_confirmation = None
            # The capture may have finished behind the dialog. Escape then
            # dismisses it without hiding the last window or acquiring a hold.
            if not any(capture_targets(app)):
                return
            if not getattr(app, "_capture_background", False):
                app.hold()
                app._capture_background = True
                GLib.timeout_add(100, refresh_capture_background, app)
            for target in targets:
                target.set_visible(False)
            return
        app._capture_confirmation = True  # Finalization guard; release the closed dialog itself.
        exiting = quitting or not any(w not in targets for w in app.get_windows())
        if exiting:
            app.begin_exit()
        app.hold()
        active_native = [target for target in native if recorder_busy(target.recorder)]
        waiting = {id(target.recorder) for target in active_native}
        browser_done = not browser
        paths = [
            str(target.recorder.session.directory)
            for target in native
            if target not in active_native and getattr(target.recorder, "session", None)
        ]
        completing = False
        completed = False
        handlers = {}
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-close-save")

        def disconnect():
            for recorder, signals in handlers.values():
                for signal in signals:
                    recorder.disconnect(signal)
            handlers.clear()

        def abort():
            nonlocal completed
            if completed:
                return
            completed = True
            disconnect()
            executor.shutdown(wait=False, cancel_futures=True)
            app._capture_confirmation = None
            app._quitting = False
            window.present()
            app.release()

        def ready():
            nonlocal completing
            if waiting or not browser_done or completing or completed:
                return
            completing = True

            def finish_saved():
                if exiting and app.transcription_enabled:
                    for path in paths:
                        queue_saved_for_later(path)

            future = executor.submit(finish_saved)

            def saved(result):
                executor.shutdown(wait=False)

                def close_saved():
                    nonlocal completed
                    if completed:
                        return False
                    try:
                        result.result()
                    except Exception:
                        # Audio is finalized; keep the window for diagnosis.
                        abort()
                    else:
                        completed = True
                        disconnect()
                        app._capture_confirmation = None
                        finish_close(app, targets)
                        if exiting:
                            app.quit()
                        app.release()
                    return False

                GLib.idle_add(close_saved)

            future.add_done_callback(saved)

        for target in active_native:
            recorder = target.recorder
            target.open_result = False

            def finished(active, path):
                waiting.discard(id(active))
                paths.append(path)
                ready()

            def failed(_active, _message):
                abort()

            handlers[id(recorder)] = (
                recorder,
                [recorder.connect("finished", finished), recorder.connect("error", failed)],
            )
        for target in active_native:
            try:
                target.recorder.stop()
            except Exception:
                abort()
                break
        if browser and not completed:
            future = executor.submit(stop_browser_captures)

            def stopped(result):
                def delivered():
                    nonlocal browser_done
                    if completed:
                        return False
                    try:
                        captured = result.result()
                    except Exception:
                        abort()
                        return False
                    paths.extend(captured)
                    if not exiting and app.transcription_enabled:
                        for path in captured:
                            app.write_down(path)
                    browser_done = True
                    ready()
                    return False

                GLib.idle_add(delivered)

            future.add_done_callback(stopped)
        ready()

    dialog.connect("response", response)
    dialog.present(window)
    return True


def finish_close(app, windows):
    app._capture_close_allowed = True
    try:
        for target in windows:
            if hasattr(target, "open_result"):
                target.open_result = False
            target.close()
            if target in app.get_windows() and not target.get_visible():
                cleanup = getattr(target, "_close", None)
                if cleanup is None or not cleanup():
                    target.destroy()
    finally:
        app._capture_close_allowed = False
    if getattr(app, "_capture_background", False):
        app._capture_background = False
        app.release()


def stop_browser_captures():
    from . import browser_service

    state = browser_service.request()
    paths = [s["path"] for s in state.get("statuses", []) if s.get("recording") and isinstance(s.get("path"), str)]
    for meeting in state.get("meetings", []):
        if meeting.get("recording"):
            browser_service.request("stop", id=meeting["id"])

    return paths


def queue_saved_for_later(path):
    """On explicit exit, publish resumable intent without starting inference."""
    from pathlib import Path

    from .library import library_root, meeting_id
    from .meeting_store import LOCK, read_metadata, update_metadata

    if not path:
        return
    directory = Path(path).resolve()
    if directory.parent != library_root().resolve() or not meeting_id(directory):
        return
    with LOCK:
        value = read_metadata(directory)
        if value["transcript"]["status"] in ("none", "failed"):
            from .library import LibraryScanner

            meeting = next(
                (
                    m
                    for m in LibraryScanner(directory.parent).scan(changed_directories={directory}).meetings
                    if m.directory == directory
                ),
                None,
            )
            if meeting and meeting.has_audio and (meeting.origin != "own" or any(t.frames for t in meeting.tracks)):
                update_metadata(
                    directory,
                    transcript=dict(status="queued", reference=value["transcript"]["reference"]),
                    etaMinutes=None,
                    error=None,
                )
