#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual GApplication process exits: busy inference, stop/save and start/stop race.

The test-recording modes (D60) exit while a test's recorder prepares or its
folder is made, also with a second launch during that exit, then check the
cache once the process has exited.
"""

import faulthandler
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
TEST_RECORDING_MODES = ("test-recording-quit", "test-recording-close", "test-recording-reactivate")
# These clocks include recording publication and arbitrary background work,
# after the window has already hidden. The enclosing 12 s process budget is
# their hang guard; 0.8 s remains a latency check for direct Quit/close modes.
BACKGROUND_COMPLETION_MODES = (
    "keep-hidden-finish",
    "keep-hidden-writing",
    "keep-browser-finish",
    "keep-browser-unscanned",
)
BACKGROUND_WRITING_MODES = (
    "keep-hidden-writing",
    "keep-browser-finish",
    "keep-browser-player",
    "keep-browser-companion",
    "keep-browser-unscanned",
)


def test_recording_exit(mode):
    """Release the child's held test once its exit began, then check its cache after it exited."""
    with tempfile.TemporaryDirectory() as cache:
        child = subprocess.Popen(
            [sys.executable, __file__, mode],
            env=dict(os.environ, XDG_CACHE_HOME=cache),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            # held: GTK still runs after the exit request; ran: run() returned.
            markers, output = (b"test-recording: held", b"test-recording: ran"), b""
            deadline = time.monotonic() + 10
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while not any(marker in output.splitlines() for marker in markers):
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and selector.select(remaining), f"{mode}: no exit began: {output!r}"
                    chunk = os.read(child.stdout.fileno(), 4096)
                    assert chunk, f"{mode}: exited before its test was released: {output!r}"
                    output += chunk
            child.stdin.write(b"release\n")
            child.stdin.flush()
            output += child.communicate(timeout=10)[0]
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        root = Path(cache) / "cassini-talk/test"
        left = sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []
        assert not left, f"{mode}: the exited process left {left}"
        assert child.returncode == 0, f"{mode}: exit {child.returncode}: {output!r}"
        print(output.decode(), end="", flush=True)


if len(sys.argv) == 1:
    for mode in (
        "quit-writing",
        "close-writing",
        "stop-save",
        "stop-starting",
        "prepare-failed",
        "stop-after-finish",
        "keep-reactivate",
        "keep-finished",
        "keep-hidden-finish",
        "keep-hidden-writing",
        "keep-browser-finish",
        "keep-browser-player",
        "keep-browser-companion",
        "keep-browser-unscanned",
        "stop-secondary-close",
        "keep-two-windows",
        "keep-reopen-two",
        "stop-interrupted",
        "stop-finishing",
        "stop-error",
        "indicator-quit-visible",
        "indicator-quit-dialog",
    ):
        subprocess.run([sys.executable, __file__, mode], check=True, timeout=12)
    for mode in TEST_RECORDING_MODES:
        test_recording_exit(mode)
    print("PASS real process exits: resumable writing, close, finalized stop/save, canceled start, test recordings")
    raise SystemExit()

sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402

from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_store import read_metadata  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402
from cassini_talk.transcription import TranscriptionQueue  # noqa: E402
from cassini_talk.whistle_client import WhistleClient  # noqa: E402

services = DesktopServices()
mode = sys.argv[1]
faulthandler.dump_traceback_later(9)
busy_writing = mode in ("quit-writing", "close-writing")
started = time.monotonic()
phase = 0
closing_at = None
writer_observed = False
writer_released_at = None
child = None
errors = []


def callback_error(kind, value, tb):
    errors.append(str(value))
    sys.__excepthook__(kind, value, tb)


sys.excepthook = callback_error


class BusyClient(WhistleClient):
    def __init__(self, *args, **kwargs):
        # Exercise the actual client's cancellation, receive and cleanup with a
        # process that is busy for much longer than a permitted UI shutdown.
        from unittest.mock import patch

        with patch("cassini_talk.whistle_client.verify_bundle", lambda path: path):
            super().__init__(*args, **kwargs)

    def start(self):
        global child
        script = """import json,sys,threading
print(json.dumps(dict(type='ready')),flush=True)
r=json.loads(sys.stdin.readline())
print(json.dumps(dict(type='progress',trackId=r['tracks'][0]['id'],trackOrder=0,language='en',etaMinutes=1,
words=[dict(startMs=0,endMs=100,text='Already saved')])),flush=True)
threading.Event().wait()
"""
        self.process = child = subprocess.Popen(
            [sys.executable, "-u", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(child.stdout, selectors.EVENT_READ)
        self.ready = self._receive("ready")
        return self


with tempfile.TemporaryDirectory() as tmp:
    os.environ["CASSINI_LIBRARY_DIR"] = tmp
    schemas = Path(tmp) / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    app = CassiniApplication("com.gocassini.QuitTest" + mode.replace("-", ""), "test", "development")
    app.background_recording.request = lambda callback: callback(True)
    app.transcription_enabled = mode != "stop-after-finish" and (
        not mode.startswith("keep-")
        or mode
        in (
            "keep-hidden-writing",
            "keep-browser-finish",
            "keep-browser-player",
            "keep-browser-companion",
            "keep-browser-unscanned",
        )
    )
    if mode in BACKGROUND_WRITING_MODES:
        writer_entered, release_writer = threading.Event(), threading.Event()

        def finish_writer():
            global writer_released_at
            assert not app._quitting and app._transcription_holds > 0
            writer_released_at = time.monotonic()
            release_writer.set()
            return GLib.SOURCE_REMOVE

        def observe_held_writer():
            global writer_observed
            if writer_observed or not writer_entered.is_set():
                return GLib.SOURCE_REMOVE
            if app._capture_background or getattr(app, "_capture_background_saving", False):
                return GLib.SOURCE_REMOVE
            if any(w in app.get_windows() for w in getattr(app, "_capture_hidden_windows", ())):
                return GLib.SOURCE_REMOVE
            # GTK must still dispatch after hidden windows retire while the
            # writer is explicitly held. Then release it on another idle.
            assert not app._quitting and app._transcription_holds > 0
            writer_observed = True
            GLib.idle_add(finish_writer)
            return GLib.SOURCE_REMOVE

        app.connect("window-removed", lambda *_: GLib.idle_add(observe_held_writer))

        class CompletedClient:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return self

            def process_tracks(self, tracks, progress):
                writer_entered.set()
                GLib.idle_add(observe_held_writer)
                # A guard below the 9 s traceback / 12 s process budgets,
                # not an inference latency requirement.
                assert release_writer.wait(8), "GTK did not observe held background writing"
                assert not app._quitting
                return dict(words=[], languages={}, diagnostics=[])

            def close(self):
                pass

            def cancel(self):
                release_writer.set()

        app.transcriber = TranscriptionQueue(
            lambda _: None,
            lambda _: GLib.idle_add(app._transcription_finished, 0),
            bundle=Path(tmp),
            client_factory=CompletedClient,
        )
    session = None
    if busy_writing:
        session = RecordingSession(tmp, source="gnome")
        session.add_track("mic", "Me", "microphone", 48000, 1)
        session.append("mic", bytes(9600), 0)
        session.finish()
        app.transcriber = TranscriptionQueue(
            lambda _: None,
            lambda _: GLib.idle_add(app._transcription_finished, 0),
            bundle=Path(tmp),
            client_factory=BusyClient,
        )

    finish_gate = None
    if mode == "stop-finishing":
        finish_gate = threading.Event()
        original_finish = RecordingSession.finish

        def slow_finish(session, *args, **kwargs):
            assert finish_gate.wait(5), "Finalization was not released"
            return original_finish(session, *args, **kwargs)

        RecordingSession.finish = slow_finish

    if mode in TEST_RECORDING_MODES:
        from cassini_talk import sound_check
        from cassini_talk.native_recorder import NativeRecorder

        def exit_now():
            global closing_at
            closing_at = time.monotonic()
            app.on_quit() if mode == "test-recording-quit" else app.get_active_window().close()
            if mode == "test-recording-reactivate":
                app.activate()  # A second launch while the hold delays the exit.
                if any(w.get_visible() for w in app.get_windows()):
                    errors.append("A launch during the exit opened a window")
                    app.quit()  # That window would keep the process: end it here.
            # Runs only while GTK still runs after the exit: something holds the app.
            GLib.idle_add(lambda: print("test-recording: held", flush=True) or False)
            return False

        def gated(work):
            def run(*args):
                GLib.idle_add(exit_now)  # Quit or close while this still runs.
                assert sys.stdin.readline() == "release\n", "The runner did not release the test"
                return work(*args)

            return run

        if mode == "test-recording-quit":
            NativeRecorder._prepare = staticmethod(gated(NativeRecorder._prepare))
        else:
            sound_check.make_folder = gated(sound_check.make_folder)

    def respond(choice):
        dialog = app._capture_confirmation
        dialog.emit("response", choice)
        # Emitting a signal alone does not dismiss an AdwDialog. Real button
        # activation closes it; reproduce both effects in this process probe.
        dialog.force_close()

    def drive():
        global phase, closing_at
        assert time.monotonic() - started < 10, "Process shutdown stalled"
        if not app.get_windows():
            return True
        window = app.get_active_window()
        if busy_writing:
            if phase == 0:
                app.write_down(session.directory)
                phase = 1
            elif (session.directory / "transcript.json").exists() and b"Already saved" in (
                session.directory / "transcript.json"
            ).read_bytes():
                closing_at = time.monotonic()
                app.on_quit() if mode == "quit-writing" else window.close()
                return False
        elif mode in ("keep-browser-finish", "keep-browser-player", "keep-browser-companion", "keep-browser-unscanned"):
            if phase == 0:
                browser_session = RecordingSession(tmp, source="chromium")
                browser_session.add_track("remote", "Remote", "remote", 48000, 1)
                browser_session.append("remote", bytes(19200), 0)
                app.test_browser_session = browser_session
                from types import SimpleNamespace

                window._activity_source = SimpleNamespace(close=lambda: None, connected=True, snapshot={})
                window._activity_source.snapshot = {browser_session.manifest["id"]: "recording"}
                window._browser_snapshot = dict(statuses=[dict(recording=True, path=str(browser_session.directory))])
                if mode in ("keep-browser-player", "keep-browser-companion"):
                    if mode == "keep-browser-companion":
                        companion = app.on_companion_setup(parent=window)
                        companion.force_close()
                        app.test_player_window = app.new_window()
                        app.test_player_window.present()
                    else:
                        app.test_player_window = app.new_window()
                        app.test_player_window.present()
                    window.close()
                elif mode == "keep-browser-unscanned":
                    window._activity_source.paths = {browser_session.manifest["id"]: str(browser_session.directory)}
                    window._browser_snapshot = {}
                    assert browser_session.directory not in [
                        m.directory for m in getattr(window.snapshot, "meetings", ())
                    ]
                    app.on_quit()
                else:
                    app.on_quit()
                respond("keep")
                assert app._capture_background and not window.get_visible()
                app.test_hidden_window = window
                if mode in ("keep-browser-player", "keep-browser-companion"):
                    from cassini_talk.capture_guard import refresh_capture_background

                    assert refresh_capture_background(app)
                    assert window in app.get_windows(), "Only browser observer was orphaned"

                browser_session.finish()
                assert str(browser_session.directory) in app._capture_paths
                window._activity_source.snapshot = {}
                window._activity_source.paths = {}
                window._browser_snapshot = {}
                if mode in ("keep-browser-player", "keep-browser-companion"):
                    phase = 1
                    return True
                closing_at = time.monotonic()
                return False
            if phase == 1 and app.test_hidden_window not in app.get_windows():
                if read_metadata(app.test_browser_session.directory)["transcript"]["status"] != "done":
                    return True
                assert not app._capture_background and app.test_player_window.get_visible()
                closing_at = time.monotonic()
                app.on_quit()
                return False
        elif mode == "stop-error":
            from cassini_talk.recording_window import RecordingWindow

            if phase == 0:
                legacy = RecordingWindow(app)
                legacy.present()
                legacy.recorder.start(Path(tmp) / "legacy.opus", test=True)
                legacy.recorder.stop = lambda: legacy.recorder._fail("Synthetic stop failure")
                app.on_quit()
                respond("stop")
                # Error handling checks for a final finished signal on the next idle.
                while GLib.MainContext.default().pending():
                    GLib.MainContext.default().iteration(False)
                assert app._capture_confirmation is None and not app._quitting
                assert legacy.get_visible() and legacy.status.get_label().startswith("Synthetic stop failure")
                assert legacy.recorder.pipeline is None
                closing_at = time.monotonic()
                app.on_quit()
                return False
        elif mode in TEST_RECORDING_MODES:
            assert app.start_test_recording(test=True)
            return False
        elif phase == 0:
            if mode == "prepare-failed":
                from cassini_talk.native_recorder import NativeRecorder

                def fail(_parent):
                    raise OSError("Synthetic preparation failure")

                NativeRecorder._prepare = staticmethod(fail)
            app.start_native(test=True)
            if mode in ("keep-two-windows", "keep-reopen-two"):
                from cassini_talk.library_window import LibraryWindow

                LibraryWindow(app).present()
            phase = 1
            if mode in ("stop-starting", "prepare-failed"):
                closing_at = time.monotonic()
                app.on_quit()
                respond("stop")
                if mode == "prepare-failed":
                    phase = 4
                    return True
                return False
        elif phase == 4 and mode == "prepare-failed":
            if app._capture_confirmation is None and app.recording_error:
                assert window.get_visible() and not app._quitting
                app.on_quit()
                return False
        elif phase == 2 and mode == "stop-finishing":
            if app.recorder._finishing:
                app.test_primary_recorder = app.recorder
                finishing_session = app.recorder.session
                app.start_native(test=True)
                independent = next(r for r in app.native_captures.values() if r is not app.test_primary_recorder)
                assert independent.starting and len(app.native_captures) == 2
                assert app.test_primary_recorder.session is finishing_session
                closing_at = time.monotonic()
                respond("stop")
                sentinel = app._capture_confirmation
                assert sentinel is not None and app.confirm_capture_close(window)
                assert app._capture_confirmation is sentinel
                finish_gate.set()
                return False
        elif phase == 3 and mode == "keep-reactivate":
            if app.recorder.pipeline is None:
                assert not getattr(app, "_capture_background", False)
                closing_at = time.monotonic()
                window.close()
                return False
        elif phase == 2 and mode.startswith("keep-"):
            if mode == "keep-reactivate":
                assert not window.get_visible() and app._capture_background
                app.activate()
                assert not app._capture_background
                app.stop_native()
                phase = 3
            elif mode in ("keep-hidden-finish", "keep-hidden-writing"):
                closing_at = time.monotonic()
                app.stop_native()
                return False
            elif mode in ("keep-two-windows", "keep-reopen-two"):
                app.activate()
                # The timer must destroy hidden auxiliary windows.
                phase = 5
            elif app.recorder.pipeline is None:
                respond("keep")
                assert not getattr(app, "_capture_background", False) and window.get_visible()
                closing_at = time.monotonic()
                window.close()
                return False
        elif phase == 5:
            from cassini_talk.library_window import LibraryWindow

            visible = [w for w in app.get_windows() if w.get_visible()]
            if len(app.get_windows()) == 1 and isinstance(visible[0], LibraryWindow):
                assert not app._capture_background
                app.stop_native()
                phase = 3
        elif phase == 3 and mode in ("keep-two-windows", "keep-reopen-two"):
            if not app.recorder.pipeline and not app.recorder._finishing:
                closing_at = time.monotonic()
                window.close()
                return False
        elif phase == 2 and mode == "stop-after-finish":
            if app.recorder.pipeline is None:
                closing_at = time.monotonic()
                respond("stop")
                return False
        elif (
            phase == 1
            and app.recorder.session
            and not app.recorder.starting
            and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        ):
            if mode in ("indicator-quit-visible", "indicator-quit-dialog"):
                # The top-bar Quit while the Library is open (perhaps behind
                # the meeting), and while its own close dialog is unanswered.
                assert window.get_visible()
                if mode == "indicator-quit-dialog":
                    window.close()
                    assert app._capture_confirmation is not None and app.recorder.pipeline is not None
                closing_at = time.monotonic()
                app.activate_action("stop-and-quit", None)
                return False
            if mode == "stop-interrupted":
                original_begin = app.recorder._begin_finish
                app.recorder._begin_finish = lambda problem=None: original_begin("Synthetic interrupted finalization")
            if mode == "stop-secondary-close":
                from cassini_talk.library_window import LibraryWindow

                LibraryWindow(app).present()
                secondary = app.get_active_window()
                assert secondary is not window
                secondary.close()
                assert app._capture_confirmation is None and app.recorder.pipeline is not None
                app.on_quit()
            elif mode == "keep-two-windows":
                # Closing an auxiliary window now needs no capture dialog.
                # Quit still offers Keep and hides all windows as before.
                app.on_quit()
            else:
                app.on_quit()
            if mode == "stop-finishing":
                # Enter held finalization through Stop's EOS drain. Calling
                # _begin_finish directly aborts live queued PCM, correctly
                # producing D69 writer loss instead of a completed session.
                app.stop_native()
                phase = 2
                return True
            if mode.startswith("keep-"):
                assert app._capture_confirmation.get_close_response() == "cancel"
                if mode == "keep-finished":
                    app.stop_native()
                else:
                    respond("keep")
                phase = 2
                return True
            if mode == "stop-after-finish":
                app.stop_native()
                phase = 2
                return True
            closing_at = time.monotonic()
            respond("stop")
            return False
        return True

    queue_before_exit = app.transcriber
    queue_closed = threading.Event()
    if busy_writing:
        reap_after_jobs = queue_before_exit._reap_after_jobs

        def reaped(reap):
            try:
                reap_after_jobs(reap)
            finally:
                queue_closed.set()

        queue_before_exit._reap_after_jobs = reaped
    GLib.timeout_add(10, drive)
    result = app.run([sys.argv[0]])
    exited_at = time.monotonic()
    if mode in TEST_RECORDING_MODES:
        if mode == "test-recording-quit":
            app.sound_check._executor.shutdown(wait=True)  # Whatever the exit queued has run.
        print("test-recording: ran", flush=True)
    assert result == 0 and closing_at is not None
    exit_seconds = exited_at - closing_at
    if mode not in BACKGROUND_COMPLETION_MODES:
        assert exit_seconds < 0.8, (mode, exit_seconds)
    if mode in BACKGROUND_WRITING_MODES:
        assert writer_observed and release_writer.is_set(), "Background writing was not held alive by GTK"
    assert app._transcription_holds == 0 and not app.get_windows(), (
        app._transcription_holds,
        [(type(w).__name__, w.get_visible()) for w in app.get_windows()],
        app._quitting,
        app._capture_confirmation,
    )
    if busy_writing:
        assert read_metadata(session.directory)["transcript"]["status"] == "running"
        assert b"Already saved" in (session.directory / "transcript.json").read_bytes()
        cleanup_deadline = time.monotonic() + 2
        child.wait(timeout=2)
        # GTK is already gone; wait within the same two-second cleanup budget
        # rather than racing fixture removal against queued transcript writes.
        assert queue_closed.wait(max(0, cleanup_deadline - time.monotonic())), "Queue teardown did not finish"
        assert queue_before_exit.client is None, "Queue teardown retained the client"
    elif mode in (
        "stop-save",
        "stop-after-finish",
        "stop-finishing",
        "stop-interrupted",
        "stop-secondary-close",
        "indicator-quit-visible",
        "indicator-quit-dialog",
    ):
        recorder = app.test_primary_recorder if mode == "stop-finishing" else app.recorder
        assert recorder.pipeline is None and not app.native_captures
        assert recorder.session.manifest["state"] == ("interrupted" if mode == "stop-interrupted" else "completed"), (
            recorder.session.manifest
        )
        assert all(t["frames"] >= 9600 for t in recorder.session.manifest["tracks"])
        assert read_metadata(recorder.session.directory)["transcript"]["status"] == (
            "none" if mode == "stop-after-finish" else "queued"
        )
        assert app.transcriber is None, "Quit started hidden inference"
    elif mode in ("keep-browser-finish", "keep-browser-player", "keep-browser-companion", "keep-browser-unscanned"):
        assert not app._capture_background
        assert read_metadata(app.test_browser_session.directory)["transcript"]["status"] == "done"
    elif mode.startswith("keep-"):
        assert not app._capture_background and app.recorder.pipeline is None
        if mode == "keep-hidden-writing":
            assert read_metadata(app.recorder.session.directory)["transcript"]["status"] == "done"
    elif mode == "stop-error":
        pass
    elif mode == "prepare-failed":
        assert app.recorder.pipeline is None and app.recorder.session is None
    elif mode in TEST_RECORDING_MODES:
        assert not app.native_captures and app.sound_check.closed and not app.sound_check.held
    else:
        assert app.recorder.pipeline is None
        if app.recorder.discarded:
            # D58: stopped before any side opened, so no recording and no folder.
            assert not app.recorder.session.directory.exists()
        else:
            assert all(t["frames"] == 0 for t in app.recorder.session.manifest["tracks"])
            assert read_metadata(app.recorder.session.directory)["transcript"]["status"] == "none"
    assert not errors, errors
    faulthandler.cancel_dump_traceback_later()
    print(
        json.dumps(
            dict(
                mode=mode,
                exitSeconds=exit_seconds,
                holds=app._transcription_holds,
                backgroundCompletion=mode in BACKGROUND_COMPLETION_MODES,
                writerReleaseToExitSeconds=exited_at - writer_released_at if writer_released_at is not None else None,
            )
        )
    )
