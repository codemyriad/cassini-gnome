#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real GTK recording/session controls and quit finalization inside the test display."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

if not os.environ.get("CASSINI_ISOLATED_AUDIO") or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
parser = argparse.ArgumentParser()
parser.add_argument("--build", required=True)
args = parser.parse_args()
build = Path(args.build).resolve()
schema_dir = build / "recording-test-schemas"
schema_dir.mkdir(exist_ok=True)
shutil.copyfile(
    Path(__file__).resolve().parents[2] / "data/com.gocassini.Viewer.gschema.xml",
    schema_dir / "com.gocassini.Viewer.gschema.xml",
)
subprocess.run(["glib-compile-schemas", str(schema_dir)], check=True)
os.environ["GSETTINGS_SCHEMA_DIR"] = str(schema_dir)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio  # noqa: E402
from readiness import PCMProgress, wait_until  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-viewer.gresource")))
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402
from cassini_viewer.recording_window import RecordingWindow  # noqa: E402


def painted(window):
    frames = []

    def tick(widget, _clock):
        if widget.get_mapped() and widget.get_width() > 0 and widget.get_height() > 0:
            frames.append(True)
        return len(frames) < 2

    callback = window.add_tick_callback(tick)
    try:
        wait_until(lambda: len(frames) >= 2, "window allocated and painted", diagnostics=diagnostics)
    finally:
        if len(frames) < 2:
            window.remove_tick_callback(callback)


artifacts = Path(os.environ.get("CASSINI_TEST_ARTIFACTS", str(Path(__file__).parent / "artifacts/gtk")))
artifacts.mkdir(parents=True, exist_ok=True)
app = CassiniApplication("com.gocassini.RecordingTest", "test", "development")
evidence = {"passed": False}


def diagnostics():
    return {
        "windows": [
            dict(type=type(w).__name__, mapped=w.get_mapped(), width=w.get_width(), height=w.get_height())
            for w in app.get_windows()
        ]
    }


try:
    app.register(None)
    app.activate()
    app.on_record()
    window = next(w for w in app.get_windows() if isinstance(w, RecordingWindow))
    assert window.button.get_label() == "Record…"
    app.on_record()
    assert sum(isinstance(w, RecordingWindow) for w in app.get_windows()) == 1
    window.microphone.set_active(False)
    window.desktop.set_active(False)
    window._clicked()
    assert window.status.get_label() == "Select at least one audio source."
    window.microphone.set_active(True)
    window.desktop.set_active(True)
    painted(window)
    subprocess.run(["import", "-window", "root", str(artifacts / "recording.png")], check=True)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "quit.opus"
        window.recorder.start(path, test=True)
        progress = PCMProgress(window.recorder.pipeline.get_by_name("capture").get_static_pad("src"))
        wait_until(lambda: progress.reached(0.4), "quit test captures actual PCM", diagnostics=diagnostics)
        progress.close()
        app.on_quit()
        assert app._capture_confirmation
        app._capture_confirmation.emit("response", "stop")
        wait_until(
            lambda: not app.get_windows(), "quit flushes and closes windows", timeout=12, diagnostics=diagnostics
        )
        assert not app.get_windows()
        assert path.read_bytes().startswith(b"OggS")
        session = RecordingSession(directory, "Browser test meeting")
        session.add_track("a", "Remote source", "remote", 48000, 1)
        session.append("a", bytes(96000), 250)
        session.finish()
        app.open_in_new_window(Gio.File.new_for_path(str(session.directory / "session.json")))
        wait_until(
            lambda: any(isinstance(w, LibraryWindow) and w.meeting_page for w in app.get_windows()),
            "recording folder opens the pushed Meeting page",
            diagnostics=diagnostics,
        )
        viewer = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
        painted(viewer)
        assert viewer.meeting_page.play.get_sensitive()
        assert viewer.meeting_page.player.duration == 1250  # 250 ms source offset + 1 second PCM
        viewer.meeting_page.play.emit("clicked")
        wait_until(
            lambda: viewer.meeting_page.player.position > 0,
            "session playback advances",
            timeout=3,
            diagnostics=diagnostics,
        )
        assert viewer.meeting_page.player.position > 0
        assert viewer.meeting_page.play.get_tooltip_text() == "Pause"
        viewer.meeting_page.play.emit("clicked")
        assert not viewer.meeting_page.player.playing
        subprocess.run(["import", "-window", "root", str(artifacts / "session.png")], check=True)
        viewer.close()
    print("PASS GTK recording controls, quit finalization, session opening and playback")
    evidence["passed"] = True
except BaseException:
    evidence["failure"] = traceback.format_exc()
    subprocess.run(["import", "-window", "root", str(artifacts / "failure.png")], check=False)
    raise
finally:
    evidence["readiness"] = diagnostics()
    (artifacts / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
