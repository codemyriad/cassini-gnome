#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive production D39 widgets in an isolated display; never install through a profile edit."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Private test display required")
parser = argparse.ArgumentParser()
parser.add_argument("--build", required=True)
parser.add_argument("--bundle", required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
schemas = Path(os.environ["XDG_RUNTIME_DIR"]) / "companion-schemas"
schemas.mkdir(exist_ok=True)
shutil.copyfile(root / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(root / "src"))
Gio.resources_register(Gio.Resource.load(str(Path(args.build) / "src/cassini-viewer.gresource")))
from cassini_viewer import companion_bundle  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402

companion_bundle.bundle_directory = lambda: Path(args.bundle)
app = CassiniApplication("com.gocassini.CompanionInstallTest", "test", "development")


def dispatch(packet):
    try:
        window = app.get_active_window()
        action = packet.get("action", "state")
        if action == "add":
            app.on_preferences()
            settings = app.settings_dialog
            settings.set_visible_page_name("browserCompanion")
            assert settings.browser_add.get_label() == "Add from Cassini" and settings.browser_add.get_sensitive()
            settings.browser_add.emit("clicked")
        setup = getattr(app, "companion_setup", None)
        guide = getattr(setup, "local_dialog", None)
        if action == "open":
            assert guide and not guide.closed and guide.open_button.get_sensitive()
            guide.open_button.emit("clicked")
        elif action == "copy":
            assert guide and not guide.closed
            guide.copy_button.emit("clicked")
        elif action == "done":
            assert guide.done.get_sensitive()
            guide.done.emit("clicked")
        elif action == "quit":
            app.on_quit()
        elif action == "shot":
            ticks = []
            window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 3)[1])
            window.queue_draw()

            def render():
                if len(ticks) < 3:
                    return GLib.SOURCE_CONTINUE
                paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
                paintable.snapshot(snapshot, window.get_width(), window.get_height())
                node = snapshot.to_node()
                texture = window.get_renderer().render_texture(node, node.get_bounds())
                texture.save_to_png(packet["path"])
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(20, render)
        controller = app.browser_connection
        state = controller.snapshot() if controller else None
        result = dict(
            phase=state["phase"] if state else None,
            connected=bool(state and state["phase"] == "connected"),
            guide=bool(guide and not guide.closed),
            done=bool(guide and guide.done.get_sensitive()),
            folder=str(guide.folder) if guide else None,
            primary=setup.add_button.get_label() if setup else None,
            copyReady=bool(guide and guide.copy_button.get_sensitive()),
        )
    except Exception as error:
        result = dict(error=str(error))
    print(json.dumps(result), flush=True)
    return GLib.SOURCE_REMOVE


def reader():
    for line in sys.stdin:
        GLib.idle_add(dispatch, json.loads(line))


app.connect_after("activate", lambda *_: app.get_active_window().set_default_size(1130, 730))
threading.Thread(target=reader, daemon=True).start()
app.run([])
