# SPDX-License-Identifier: GPL-3.0-or-later
"""UX2 confirmation 1: explicit stop-and-keep or background recording."""

from concurrent.futures import ThreadPoolExecutor

from gi.repository import Adw, GLib

from .strings import text


def confirm_capture_close(app, window, *, quitting=False):
    if getattr(app, "_capture_close_allowed", False):
        return False
    native = [w for w in app.get_windows() if getattr(w, "recorder", None) and w.recorder.pipeline is not None]
    browser = any(getattr(w, "_activity_source", None) and w._activity_source.snapshot for w in app.get_windows())
    browser = browser or any(
        any(s.get("recording") for s in getattr(w, "_browser_snapshot", {}).get("statuses", []))
        for w in app.get_windows()
    )
    if not native and not browser:
        return False
    if getattr(app, "_capture_confirmation", None):
        return True
    dialog = Adw.AlertDialog(heading=text("confirm.recordingRunning.title"), body=text("confirm.recordingRunning.body"))
    dialog.add_response("keep", text("confirm.recordingRunning.secondary"))
    dialog.add_response("stop", text("confirm.recordingRunning.primary"))
    dialog.set_default_response("stop")
    dialog.set_close_response("keep")
    app._capture_confirmation = dialog

    def close_windows():
        finish_close(app, list(app.get_windows()) if quitting else [window])
        return False

    def response(_dialog, choice):
        app._capture_confirmation = None
        if choice == "keep":
            if not getattr(app, "_capture_background", False):
                app.hold()
                app._capture_background = True
            for target in app.get_windows() if quitting else [window]:
                target.set_visible(False)
            return
        for target in native:
            target.open_result = False
            target.recorder.stop()
        if not browser:
            GLib.idle_add(close_windows)
            return

        def stop_browser():
            from . import browser_service

            state = browser_service.request()
            for meeting in state.get("meetings", []):
                if meeting.get("recording"):
                    browser_service.request("stop", id=meeting["id"])

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(stop_browser)

        def stopped(result):
            executor.shutdown(wait=False)
            try:
                result.result()
            except (OSError, ValueError):
                # Keep the controller/window available if stopping failed.
                GLib.idle_add(window.present)
            else:
                GLib.idle_add(close_windows)

        future.add_done_callback(stopped)

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
