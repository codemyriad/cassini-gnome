#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual GApplication process exits: busy inference, stop/save and start/stop race."""

import json
import os
import selectors
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
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import read_metadata  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402
from cassini_viewer.transcription import TranscriptionQueue  # noqa: E402
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402

mode = sys.argv[1]
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
    app = CassiniApplication("com.gocassini.QuitTest" + mode.replace("-", ""), "test", "development")
    app.transcription_enabled = mode != "stop-after-finish" and not mode.startswith("keep-")
    session = None
    if mode.endswith("writing"):
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
        if mode.endswith("writing"):
            if phase == 0:
                app.write_down(session.directory)
                phase = 1
            elif (session.directory / "transcript.json").exists() and b"Already saved" in (
                session.directory / "transcript.json"
            ).read_bytes():
                closing_at = time.monotonic()
                app.on_quit() if mode == "quit-writing" else window.close()
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
                finishing_session = app.recorder.session
                app.start_native(test=True)
                assert app.recorder.session is finishing_session and not app.recorder.starting
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
            elif mode == "keep-hidden-finish":
                closing_at = time.monotonic()
                app.stop_native()
                return False
            elif app.recorder.pipeline is None:
                respond("keep")
                assert not getattr(app, "_capture_background", False) and window.get_visible()
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
            app.on_quit()
            if mode == "stop-finishing":
                app.recorder._begin_finish()
                phase = 2
                return True
            if mode.startswith("keep-"):
                assert app._capture_confirmation.get_close_response() == "keep"
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

    GLib.timeout_add(10, drive)
    result = app.run([sys.argv[0]])
    assert result == 0 and closing_at is not None and time.monotonic() - closing_at < 0.8
    assert app._transcription_holds == 0 and not app.get_windows(), (
        app._transcription_holds,
        [(type(w).__name__, w.get_visible()) for w in app.get_windows()],
        app._quitting,
        app._capture_confirmation,
    )
    if mode.endswith("writing"):
        assert read_metadata(session.directory)["transcript"]["status"] == "running"
        assert b"Already saved" in (session.directory / "transcript.json").read_bytes()
        deadline = time.monotonic() + 2
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert child.poll() is not None
    elif mode in ("stop-save", "stop-after-finish", "stop-finishing"):
        assert app.recorder.pipeline is None
        assert app.recorder.session.manifest["state"] == "completed", app.recorder.session.manifest
        assert all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        assert read_metadata(app.recorder.session.directory)["transcript"]["status"] == (
            "none" if mode == "stop-after-finish" else "queued"
        )
        assert app.transcriber is None, "Quit started hidden inference"
    elif mode.startswith("keep-"):
        assert not app._capture_background and app.recorder.pipeline is None
    elif mode == "stop-error":
        pass
    elif mode == "prepare-failed":
        assert app.recorder.pipeline is None and app.recorder.session is None
    else:
        assert app.recorder.pipeline is None
        assert all(t["frames"] == 0 for t in app.recorder.session.manifest["tracks"])
        assert read_metadata(app.recorder.session.directory)["transcript"]["status"] == "none"
    assert not errors, errors
    print(json.dumps(dict(mode=mode, exitSeconds=time.monotonic() - closing_at, holds=app._transcription_holds)))
