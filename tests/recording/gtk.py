#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real GTK recording/session controls and quit finalization inside the test display."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
from gi.repository import Gio, GLib  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-viewer.gresource")))
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402
from cassini_viewer.recording_window import RecordingWindow  # noqa: E402
from cassini_viewer.session_window import SessionWindow  # noqa: E402

context = GLib.MainContext.default()


def spin(seconds, until=lambda: False):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not until():
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


artifacts = Path(__file__).parent / "artifacts/gtk"
artifacts.mkdir(parents=True, exist_ok=True)
app = CassiniApplication("com.gocassini.RecordingTest", "test", "development")
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
spin(0.5)
subprocess.run(["import", "-window", "root", str(artifacts / "recording.png")], check=True)
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "quit.opus"
    window.recorder.start(path, test=True)
    spin(0.4)
    app.on_quit()
    spin(12, lambda: not app.get_windows())
    assert not app.get_windows()
    assert path.read_bytes().startswith(b"OggS")
    session = RecordingSession(directory, "Browser test meeting")
    session.add_track("a", "Remote source", "remote", 48000, 1)
    session.append("a", bytes(96000), 250)
    session.finish()
    app.open_in_new_window(Gio.File.new_for_path(str(session.directory / "session.json")))
    viewer = next(w for w in app.get_windows() if isinstance(w, SessionWindow))
    spin(0.4)
    assert viewer.play.get_sensitive()
    assert viewer.player.duration == 1000
    viewer.play.emit("clicked")
    spin(3, lambda: viewer.player.position > 0)
    assert viewer.player.position > 0
    assert viewer.play.get_label() == "Pause"
    viewer.play.emit("clicked")
    assert not viewer.player.playing
    subprocess.run(["import", "-window", "root", str(artifacts / "session.png")], check=True)
    viewer.close()
print("PASS GTK recording controls, quit finalization, session opening and playback")
