#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive real GNOME widgets and notifications inside the private recording harness."""

import argparse
import json
import os
import shutil
import sys
import threading
from pathlib import Path

if not os.environ.get("CASSINI_ISOLATED_AUDIO") or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Private harness required")
parser = argparse.ArgumentParser()
parser.add_argument("--build", required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
build = Path(args.build).resolve()
schemas = build / "browser-test-schemas"
schemas.mkdir(exist_ok=True)
shutil.copyfile(root / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
import subprocess  # noqa: E402

subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

sys.path.insert(0, str(root / "src"))
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-viewer.gresource")))
from cassini_viewer.browser_window import BrowserMeetingsWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402

app = CassiniApplication("com.gocassini.Viewer.BrowserTest", "test", "development")
app.hold()  # Keep only the private notification test server alive after a window closes.
notifications = {}
next_notification = 0
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
xml = """<node><interface name="org.freedesktop.Notifications">
<method name="Notify"><arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/></method>
<method name="CloseNotification"><arg type="u" direction="in"/></method>
<signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
</interface></node>"""


def notification_call(_connection, _sender, _path, _interface, method, parameters, invocation):
    global next_notification
    if method == "Notify":
        next_notification += 1
        values = parameters.unpack()
        notifications[next_notification] = dict(title=values[3], body=values[4], actions=values[5])
        invocation.return_value(GLib.Variant("(u)", (next_notification,)))
    else:
        notifications.pop(parameters.unpack()[0], None)
        invocation.return_value(None)


bus.register_object(
    "/org/freedesktop/Notifications", Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], notification_call
)
Gio.bus_own_name_on_connection(bus, "org.freedesktop.Notifications", Gio.BusNameOwnerFlags.NONE, None, None)


def dispatch(packet, result, ready):
    try:
        window = next((w for w in app.get_windows() if isinstance(w, BrowserMeetingsWindow)), None)
        action = packet.get("action", "status")
        if action == "notification-stop":
            identifier = int(packet["id"])
            bus.emit_signal(
                None,
                "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications",
                "ActionInvoked",
                GLib.Variant("(us)", (identifier, "stop")),
            )
        elif action == "quit":
            app.quit()
        elif action == "reopen":
            app.activate_action("browser-meetings", None)
            window = next(w for w in app.get_windows() if isinstance(w, BrowserMeetingsWindow))
        elif window is not None:
            if action in ("record", "stop"):
                row = window.rows[packet["id"]]
                assert row.button.get_sensitive(), "Recording button is disabled"
                row.button.emit("clicked")
            elif action == "auto":
                window.automatic.set_active(packet["enabled"])
            elif action == "close-window":
                window.close()
                window = None
        if window is None:
            result.update(dict(status="Window closed", rows=[], notifications=notifications))
            return GLib.SOURCE_REMOVE
        result.update(
            dict(
                status=window.status.get_label(),
                auto=window.automatic.get_active(),
                rows=[
                    dict(
                        id=identifier,
                        title=row.get_title(),
                        button=row.button.get_label(),
                        enabled=row.button.get_sensitive(),
                    )
                    for identifier, row in window.rows.items()
                ],
                notifications=notifications,
                error=window.error.get_label() if window.error.get_visible() else None,
            )
        )
    except Exception as error:
        result["error"] = str(error)
    finally:
        ready.set()
    return GLib.SOURCE_REMOVE


def requests():
    for line in sys.stdin:
        result, ready = {}, threading.Event()
        try:
            packet = json.loads(line)
            GLib.idle_add(dispatch, packet, result, ready)
            if not ready.wait(10):
                result["error"] = "GNOME main loop did not answer"
        except Exception as error:
            result["error"] = str(error)
        print(json.dumps(result), flush=True)
        if packet.get("action") == "quit":
            return


# Launch still exercises the Library; enter the retained browser screen explicitly.
app.connect("activate", lambda application: application.activate_action("browser-meetings", None))
threading.Thread(target=requests, daemon=True).start()
sys.exit(app.run([]))
