# SPDX-License-Identifier: GPL-3.0-or-later
"""UX2 confirmation 1: explicit stop-and-keep or background recording."""

from concurrent.futures import ThreadPoolExecutor

from gi.repository import Adw, GLib

from .strings import text


def confirm_capture_close(app, window, *, quitting=False):
    if getattr(app, "_capture_close_allowed", False):
        return False
    native = [w for w in app.get_windows() if getattr(w, "recorder", None) and w.recorder.pipeline is not None]
    if getattr(app, "recorder", None) and (app.recorder.pipeline is not None or app.recorder.starting):
        native.append(app)
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

    def response(_dialog, choice):
        app._capture_confirmation = None
        targets = list(app.get_windows()) if quitting else [window]
        if choice == "keep":
            if not getattr(app, "_capture_background", False):
                app.hold()
                app._capture_background = True
            for target in targets:
                target.set_visible(False)
            return
        exiting = quitting or not any(w not in targets for w in app.get_windows())
        if exiting:
            app.begin_exit()
        # Keep windows and a guard hold until every capture has finished writing.
        app.hold()
        # A capture can finish (for example at the disk reserve) while the
        # confirmation is open. Never wait for its already-delivered signal.
        active_native = [
            target
            for target in native
            if target.recorder.pipeline is not None or getattr(target.recorder, "starting", False)
        ]
        waiting = {id(target.recorder) for target in active_native}
        browser_done = not browser
        paths = [
            str(target.recorder.session.directory)
            for target in native
            if target not in active_native and getattr(target.recorder, "session", None)
        ]
        completing = False
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-close-save")

        def ready():
            nonlocal completing
            if waiting or not browser_done or completing:
                return
            completing = True

            def finish_saved():
                if exiting:
                    for path in paths:
                        queue_saved_for_later(path)

            future = executor.submit(finish_saved)

            def saved(result):
                executor.shutdown(wait=False)

                def close_saved():
                    try:
                        result.result()
                    except (OSError, ValueError):
                        # Audio is finalized; keep the window for diagnosis.
                        app._quitting = False
                        window.present()
                    else:
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

            def finished(active, path, *, recorder=recorder):
                active.disconnect(handlers.pop(id(active)))
                waiting.discard(id(active))
                paths.append(path)
                ready()

            handlers[id(recorder)] = recorder.connect("finished", finished)
        for target in active_native:
            target.recorder.stop()
        if browser:
            future = executor.submit(stop_browser_captures)

            def stopped(result):
                def delivered():
                    nonlocal browser_done
                    try:
                        completed = result.result()
                    except (OSError, ValueError):
                        executor.shutdown(wait=False)
                        app._quitting = False
                        window.present()
                        app.release()
                        return False
                    paths.extend(completed)
                    if not exiting and getattr(app, "transcription_enabled", False):
                        for path in completed:
                            app.write_down(path)
                    browser_done = True
                    ready()
                    return False

                GLib.idle_add(delivered)

            future.add_done_callback(stopped)
        ready()

    handlers = {}
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


def stop_browser_captures(directory=None):
    from pathlib import Path

    from . import browser_service

    def matches(path):
        return isinstance(path, str) and (directory is None or Path(path).resolve() == Path(directory).resolve())

    state = browser_service.request()
    statuses = [s for s in state.get("statuses", []) if s.get("recording") and matches(s.get("path"))]
    paths = [s["path"] for s in statuses]
    identifiers = {s["id"] for s in statuses if isinstance(s.get("id"), str)}
    identifiers.update(
        m["id"]
        for m in state.get("meetings", [])
        if m.get("recording") and (directory is None or matches(m.get("path")))
    )
    for identifier in identifiers:
        browser_service.request("stop", id=identifier)
    after = browser_service.request()
    if any(s.get("recording") and matches(s.get("path")) for s in after.get("statuses", [])):
        raise ValueError("The browser has not confirmed the recording stopped")
    return paths


def queue_saved_for_later(path):
    """On explicit exit, publish resumable intent without starting inference."""
    from pathlib import Path

    from .library import library_root, meeting_id
    from .meeting_store import read_metadata, update_metadata

    if not path:
        return
    directory = Path(path).resolve()
    if directory.parent != library_root().resolve() or not meeting_id(directory):
        return
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
        if meeting and meeting.has_audio:
            update_metadata(
                directory,
                transcript=dict(status="queued", reference=value["transcript"]["reference"]),
                etaMinutes=None,
                error=None,
            )
