#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D28 real dialog, live refresh, clipboard and three 1120x720 review boards."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from details_fixture import build_library, write_json  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.main import CassiniApplication  # noqa: E402


def until(predicate):
    end = time.monotonic() + 12
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Recording details condition timed out")


def shot(window, name):
    frames = []
    window.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 3)[1])
    window.queue_draw()
    until(lambda: len(frames) >= 2)
    paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_name(f"recording-details-{name}.png")
    assert texture.save_to_png(str(target))


with tempfile.TemporaryDirectory() as temporary:
    private = Path(temporary)
    paths = build_library(private / "Library")
    live = json.loads((paths["fallback"] / "session.json").read_text())
    live["state"] = "recording"
    for track in live["tracks"]:
        if track["kind"] in ("microphone", "system"):
            track["ended"] = False
    write_json(paths["fallback"] / "session.json", live)
    # Current production writes down after capture ends. A live fixture must
    # not pretend it already has a finished transcript or selected ASR model.
    live_metadata = json.loads((paths["fallback"] / "meeting.json").read_text())
    live_metadata["transcript"] = None
    write_json(paths["fallback"] / "meeting.json", live_metadata)
    (paths["fallback"] / "transcript.json").unlink()
    write_json(private / "activity.json", {live["id"]: "recording"})
    schemas = private / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(private / "Library"),
        CASSINI_SCREENSHOT_ACTIVITY=str(private / "activity.json"),
        CASSINI_SCREENSHOT_NOW="1793267225000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Gtk.Settings.get_default().props.gtk_font_name = "Cantarell 11"
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    app = CassiniApplication("com.gocassini.RecordingDetailsTest", "draft", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending and len(window.snapshot.meetings) == 3)
    copied = []
    for key in ("fallback", "microphone", "no-events"):
        meeting = next(m for m in window.snapshot.meetings if m.directory == paths[key])
        window.open_meeting(meeting)
        window._update_live()
        if key == "fallback":
            assert window.recording_page
            window.recording_page.details_button.emit("clicked")
        else:
            assert window.meeting_page
            menu = window.meeting_page.more_menu
            assert any(
                menu.get_item_attribute_value(i, "label", None).unpack() == "Recording details…"
                for i in range(menu.get_n_items())
            )
            window.lookup_action("recording-details").activate(None)
        dialog = window.recording_details_dialog
        until(lambda dialog=dialog: dialog.report is not None)
        assert dialog.report.folder == str(paths[key])
        shot(window, {"fallback": "browser-fallback", "microphone": "microphone", "no-events": "no-events"}[key])
        body = dialog.report.plain_text()
        if key == "fallback":
            assert dialog.report.live and "3 audio streams" in body
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            shot(window, "browser-fallback-dark")
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            live["problemKind"] = "io"
            write_json(paths[key] / "session.json", live)
            until(lambda dialog=dialog: dict(dialog.report.health)["Reported issue"] == "io")
        elif key == "microphone":
            assert "-2060 PCM16" in body and "Stopped by you" in body
            opened = []

            def launch(launcher, _parent, _cancel, _callback, opened=opened):
                opened.append(launcher.get_file().get_path())

            with patch.object(Gtk.FileLauncher, "launch", launch):
                dialog.open_button.emit("clicked")
            assert opened == [str(paths[key])]
        else:
            assert "Source-change events were not recorded" in body
            dialog.copy_button.emit("clicked")
            clipboard = dialog.get_display().get_clipboard()
            clipboard.read_text_async(None, lambda c, r: copied.append(c.read_text_finish(r)))
            until(lambda: bool(copied))
            assert copied[-1] == body
        dialog.close()
        until(lambda dialog=dialog: dialog.closed)
        assert not dialog.timer
        window.back_to_library()
    # Closing during a slow read must not retain a timer, touch closed widgets,
    # block GTK or take an application hold.
    from cassini_viewer import capture_details_dialog

    entered, release = threading.Event(), threading.Event()
    original = capture_details_dialog.read_details

    def blocked(meeting):
        entered.set()
        assert release.wait(4)
        return original(meeting)

    with patch.object(capture_details_dialog, "read_details", blocked), patch.object(app, "hold") as hold:
        window.show_recording_details()
        dialog = window.recording_details_dialog
        until(entered.is_set)
        dialog.close()
        until(lambda: dialog.closed)
        assert dialog.timer == 0 and dialog.report is None
        assert not hold.called
        release.set()
    window.monitor.close()
    window.destroy()
    app.quit()
print("PASS D28 menu/link, real read-only fixtures, live refresh, clipboard and 3x1120x720 screenshots")
