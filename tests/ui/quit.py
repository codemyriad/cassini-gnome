#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual GApplication process exits: busy inference, stop/save and start/stop race."""

import faulthandler
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
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
    ):
        subprocess.run([sys.executable, __file__, mode], check=True, timeout=12)
    print("PASS real process exits: resumable busy writing, close, finalized stop/save and canceled start")
    raise SystemExit()

sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402

from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import read_metadata  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402
from cassini_viewer.transcription import TranscriptionQueue  # noqa: E402
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402

services = DesktopServices()
mode = sys.argv[1]
faulthandler.dump_traceback_later(9)
busy_writing = mode in ("quit-writing", "close-writing")
started = time.monotonic()
phase = 0
closing_at = None
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

        with patch("cassini_viewer.whistle_client.verify_bundle", lambda path: path):
            super().__init__(*args, **kwargs)

    def start(self):
        global child
        script = """import json,sys,time
print(json.dumps(dict(type='ready')),flush=True)
r=json.loads(sys.stdin.readline())
print(json.dumps(dict(type='progress',trackId=r['tracks'][0]['id'],trackOrder=0,language='en',etaMinutes=1,
words=[dict(startMs=0,endMs=100,text='Already saved')])),flush=True)
time.sleep(30)
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
    if mode in (
        "keep-hidden-writing",
        "keep-browser-finish",
        "keep-browser-player",
        "keep-browser-companion",
        "keep-browser-unscanned",
    ):

        class CompletedClient:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return self

            def process_tracks(self, tracks, progress):
                time.sleep(0.3)  # Keep inference alive beyond the background-close timer.
                assert not app._quitting
                return dict(words=[], languages={}, diagnostics=[])

            def close(self):
                pass

            def cancel(self):
                pass

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
        import threading

        finish_gate = threading.Event()
        original_finish = RecordingSession.finish

        def slow_finish(session, *args):
            assert finish_gate.wait(5), "Finalization was not released"
            return original_finish(session, *args)

        RecordingSession.finish = slow_finish

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
                    from cassini_viewer.capture_guard import refresh_capture_background

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
            from cassini_viewer.recording_window import RecordingWindow

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
        elif phase == 0:
            if mode == "prepare-failed":
                from cassini_viewer.native_recorder import NativeRecorder

                def fail(_parent):
                    raise OSError("Synthetic preparation failure")

                NativeRecorder._prepare = staticmethod(fail)
            app.start_native(test=True)
            if mode in ("keep-two-windows", "keep-reopen-two"):
                from cassini_viewer.library_window import LibraryWindow

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
            from cassini_viewer.library_window import LibraryWindow

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
            and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        ):
            if mode == "stop-interrupted":
                original_begin = app.recorder._begin_finish
                app.recorder._begin_finish = lambda problem=None: original_begin("Synthetic interrupted finalization")
            if mode == "stop-secondary-close":
                from cassini_viewer.library_window import LibraryWindow

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
                app.recorder._begin_finish()
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
    GLib.timeout_add(10, drive)
    result = app.run([sys.argv[0]])
    assert result == 0 and closing_at is not None and time.monotonic() - closing_at < 0.8
    assert app._transcription_holds == 0 and not app.get_windows(), (
        app._transcription_holds,
        [(type(w).__name__, w.get_visible()) for w in app.get_windows()],
        app._quitting,
        app._capture_confirmation,
    )
    if busy_writing:
        assert read_metadata(session.directory)["transcript"]["status"] == "running"
        assert b"Already saved" in (session.directory / "transcript.json").read_bytes()
        deadline = time.monotonic() + 2
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert child.poll() is not None
        # The latency assertion above measures GTK shutdown. The queue then
        # flushes/reaps off GTK; keep its fixture directory alive until that
        # cleanup finishes instead of racing rmtree against transcript writes.
        while queue_before_exit.client is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert queue_before_exit.client is None, "Queue teardown did not finish"
    elif mode in ("stop-save", "stop-after-finish", "stop-finishing", "stop-interrupted", "stop-secondary-close"):
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
    else:
        assert app.recorder.pipeline is None
        assert all(t["frames"] == 0 for t in app.recorder.session.manifest["tracks"])
        assert read_metadata(app.recorder.session.directory)["transcript"]["status"] == "none"
    assert not errors, errors
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(dict(mode=mode, exitSeconds=time.monotonic() - closing_at, holds=app._transcription_holds)))
