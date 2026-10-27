#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Meeting navigation/transport/items/preferences in the private display."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Gst, Gtk  # noqa: E402

build = Path(os.environ["CASSINI_TEST_BUILD"])
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-viewer.gresource")))
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402


def until(check):
    end = time.monotonic() + 8
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Meeting GUI condition timed out"


def labels(widget):
    result = [widget.get_label()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        result += labels(child)
        child = child.get_next_sibling()
    return result


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root / "fixtures/main/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(root / "fixtures/main/activity.json")
    os.environ["CASSINI_SCREENSHOT_NOW"] = "1793107080000"
    app = CassiniApplication("com.gocassini.MeetingTest", "test", "development")
    app.register(None)
    app.activate()
    window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not window._pending)
    meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    window.player.shutdown()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player = FolderPlayer(audio_sink=sink)
    window._connect_player()
    window.open_meeting(meeting)
    page = window.meeting_page
    until(lambda: len(page.bound) == 5 and window.player._prerolled)
    assert all(
        v in labels(page.speakers)
        for v in ["43%", "36%", "21%", "Named by you", "Recognised from saved voices", "Not named yet"]
    )
    window.player.seek(192000)
    current = next(row for row in page.bound.values() if row[0].speaker == "guest")
    assert "background=" in current[1].get_label()
    assert 'alpha="60%"' in page.bound[3][1].get_label()
    assert page.elapsed.get_label() == "00:03:12" and page.total.get_label() == "00:47:05"
    current[1].emit("activate-link", "item:0")
    until(lambda: window.player.position == 188000)
    page.seek.emit("change-value", Gtk.ScrollType.JUMP, 190000)
    assert window.player.position == 190000
    page.play.emit("clicked")
    until(lambda: window.player.playing)
    assert page.play.get_tooltip_text() == "Pause"
    page.play.emit("clicked")
    assert not window.player.playing
    assert page._companion.get_visible()
    page.set_connected(True)
    assert not page._companion.get_visible()
    page.set_connected(False)
    page._dismiss_card()
    window.back_to_library()
    until(lambda: window.meeting_page is None)
    window.open_meeting(meeting)
    page = window.meeting_page
    assert not page._companion.get_visible(), "Not now must persist across page instances"
    metadata = meeting.directory / "meeting.json"
    value = json.loads(metadata.read_text())
    value["transcript"]["status"] = "failed"
    value["etaMinutes"] = None
    metadata.write_text(json.dumps(value))
    until(lambda: window.meeting_page.meeting.transcript == "failed")
    assert "Could not write this meeting down. The audio is fine." in labels(page.lines)
    window.back_to_library()
    until(lambda: window.meeting_page is None)
    received = next(m for m in window.snapshot.meetings if m.origin == "received")
    app.open_in_new_window(Gio.File.new_for_path(str(received.audio_file)))
    until(lambda: window.meeting_page is not None)
    assert window.meeting_page.meeting.words
    assert len(app.get_windows()) == 1, "File opens belong to the pushed reading page"
    # Real confirmation widgets, deterministic controller double; no device access.
    recording = Adw.ApplicationWindow(application=app)

    class Controller(GObject.Object):
        __gsignals__ = {"finished": (GObject.SignalFlags.RUN_LAST, None, (str,))}
        pipeline = object()

        def stop(self):
            self.pipeline = None
            GLib.idle_add(lambda: (self.emit("finished", str(Path(temporary) / "legacy.opus")), False)[1])

    recorder = Controller()
    recording.recorder = recorder
    recording.present()
    until(recording.get_mapped)
    app.on_quit()
    assert app._capture_confirmation
    app._capture_confirmation.emit("response", "keep")
    assert recorder.pipeline is not None and not window.get_visible()
    app.activate()
    assert window.get_visible()
    app.on_quit()
    app._capture_confirmation.emit("response", "stop")
    try:
        until(lambda: not app.get_windows())
    except AssertionError:
        print([(type(w).__name__, w.get_visible()) for w in app.get_windows()], flush=True)
        raise
print(
    "Meeting GUI passed: timed reading, folder/Opus navigation, playback, card persistence, live updates and close confirmation"
)
