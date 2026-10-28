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
    """Retire hidden windows without treating background completion as Quit."""
    if not getattr(app, "_capture_background", False):
        return False
    if getattr(app, "_capture_confirmation", None):
        return True
    windows = app.get_windows()
    remember_capture_paths(app)
    native, browser = capture_targets(app)
    hidden = [w for w in getattr(app, "_capture_hidden_windows", ()) if w in windows and not w.get_visible()]
    visible = [w for w in windows if w.get_visible()]
    if visible and (native or browser):
        browser_observed = any(
            getattr(w, "_activity_source", None) is not None or hasattr(w, "_browser_snapshot") for w in visible
        )
        # A legacy player does not poll browser capture. Retain its hidden
        # observer until another observer is visible or the capture is saved.
        finished = [
            w for w in hidden if not recorder_busy(getattr(w, "recorder", None)) and (not browser or browser_observed)
        ]
        finish_close(app, finished, background=True)
        if len(finished) < len(hidden):
            return True
        app._capture_hidden_windows = set()
        release_background(app)
        return False
    if native or browser or getattr(app, "_browser_stops", ()):
        return True
    if getattr(app, "_capture_background_saving", False):
        return True
    from gi.repository import GLib

    # Browser activity can disappear before a Library scan observes saved audio.
    # Persist intent before dropping the last window/hold, then let the normal
    # queue finish under its own holds. No inference is cancelled by this close.
    app._capture_background_saving = True
    paths = tuple(getattr(app, "_capture_paths", ()))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-hidden-save")

    def save():
        if app.transcription_enabled:
            return [path for path in paths if queue_saved_for_later(path)]
        return []

    def saved(future):
        executor.shutdown(wait=False)

        def complete():
            app._capture_background_saving = False
            try:
                queued = future.result()
            except (OSError, ValueError) as error:
                app.recording_error = str(error)
                app.activate()
                return False
            if not app._quitting:
                for path in queued:
                    app.write_down(path, resume=True)
            if any(capture_targets(app)):
                # A new independent capture may have started while the old
                # saved-intent IO was running. Keep its windows and observer.
                if getattr(app, "_capture_background", False):
                    GLib.timeout_add(100, refresh_capture_background, app)
                return False
            hidden = [
                w for w in app.get_windows() if w in getattr(app, "_capture_hidden_windows", ()) and not w.get_visible()
            ]
            finish_close(app, hidden, background=True)
            app._capture_hidden_windows = set()
            app._capture_paths = set()
            release_background(app)
            return False

        GLib.idle_add(complete)

    executor.submit(save).add_done_callback(saved)
    return False


def release_background(app):
    if getattr(app, "_capture_background", False):
        app._capture_background = False
        app.release()


def remember_capture_paths(app):
    paths = getattr(app, "_capture_paths", set())
    native, _ = capture_targets(app)
    for target in native:
        session = getattr(target.recorder, "session", None)
        if session:
            paths.add(str(session.directory))
    for window in app.get_windows():
        for meeting in getattr(getattr(window, "snapshot", None), "meetings", ()):
            if meeting.lifecycle in ("recording", "saving"):
                paths.add(str(meeting.directory))
        for status in getattr(window, "_browser_snapshot", {}).get("statuses", ()):
            if status.get("recording") and isinstance(status.get("path"), str):
                paths.add(status["path"])
    app._capture_paths = paths
    return paths


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
    remember_capture_paths(app)
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
            remember_capture_paths(app)
            app._capture_hidden_windows = getattr(app, "_capture_hidden_windows", set()) | set(targets)
            if not getattr(app, "_capture_background", False):
                app.hold()
                app._capture_background = True
                GLib.timeout_add(100, refresh_capture_background, app)
            for target in targets:
                target.set_visible(False)
            return
        app._capture_confirmation = True  # Finalization guard; release the closed dialog itself.
        exiting = quitting or not any(w not in targets for w in app.get_windows())
        app._capture_exiting = exiting
        paths = list(remember_capture_paths(app))
        app.hold()
        active_native = [target for target in native if recorder_busy(target.recorder)]
        waiting = {id(target.recorder) for target in active_native}
        browser_done = not browser
        paths.extend(
            str(target.recorder.session.directory)
            for target in native
            if target not in active_native and getattr(target.recorder, "session", None)
        )
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
            app._capture_exiting = False
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
                        app._capture_exiting = False
                        if exiting:
                            app.begin_exit()
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

            def failed(active, _message):
                # NativeRecorder reports an interrupted, finalized recording
                # with error followed by finished. Legacy/start failures have
                # no finished signal: check on the next main-loop iteration.
                def check():
                    if id(active) in waiting:
                        abort()
                    return False

                GLib.idle_add(check)

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


def finish_close(app, windows, *, background=False):
    previous = getattr(app, "_closing_background_windows", False)
    app._closing_background_windows = previous or background
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
        app._closing_background_windows = previous
    if not background:
        release_background(app)


def stop_browser_captures(directory=None):
    import time
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
        deadline = time.monotonic() + 5
        while True:
            try:
                browser_service.request("stop", id=identifier)
                break
            except ValueError as error:
                # A concurrent stop may already have finished, or may still
                # be saving. Check the controller rather than aborting Quit.
                current = browser_service.request()
                active = [s for s in current.get("statuses", []) if s.get("recording") and s.get("id") == identifier]
                if not active:
                    break
                busy = str(error) == "A recording command is still being processed" or any(
                    m.get("busy") and m.get("id") == identifier for m in current.get("meetings", [])
                )
                if time.monotonic() >= deadline or not busy:
                    raise
                time.sleep(0.05)
    after = browser_service.request()
    if any(s.get("recording") and matches(s.get("path")) for s in after.get("statuses", [])):
        raise ValueError("The browser has not confirmed the recording stopped")
    return paths


def queue_saved_for_later(path):
    """On explicit exit, publish resumable intent without starting inference."""
    from pathlib import Path

    from .library_location import owned_directory
    from .meeting_store import LOCK, read_metadata, update_metadata

    if not path:
        return
    try:
        directory = owned_directory(Path(path))
    except ValueError:
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
                return True
        return value["transcript"]["status"] in ("queued", "running")
