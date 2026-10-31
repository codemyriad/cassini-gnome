#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Library actions, private portal/notifications and browser inventory."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Private harness required")
parser = argparse.ArgumentParser()
parser.add_argument("--build", required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
build = Path(args.build).resolve()
schemas = Path(os.environ["XDG_RUNTIME_DIR"]) / "browser-test-schemas"
schemas.mkdir()
shutil.copyfile(root / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml")
subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib  # noqa: E402

sys.path[:0] = [str(root / "src"), str(root / "tests")]
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-talk.gresource")))
from desktop_services import DesktopServices  # noqa: E402

from cassini_talk.global_shortcut import GlobalShortcut  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402

services = DesktopServices()
app = CassiniApplication("io.codemyriad.CassiniTalk.BrowserTest", "test", "development")
# The RPC test server stays available while checking normal windowless auto-record.
app.hold()
# Production deliberately avoids shortcut registration in isolated-audio runs.
# Bind the real adapter explicitly to this harness's private portal instead.
app.connect(
    "startup",
    lambda application: setattr(
        application,
        "global_shortcut",
        GlobalShortcut(application.on_global_record, connection=services.bus, settings=application.recording_settings),
    ),
)


def dispatch(packet, result, ready):
    try:
        from cassini_talk.capture_guard import recorder_busy

        window = next((w for w in app.get_windows() if isinstance(w, LibraryWindow)), None)
        action = packet.get("action", "status")
        if action == "quit":
            app.on_quit()
        elif action == "reopen":
            app.activate()
        elif action == "notification-stop":
            services.app_action(app, "stop-recording")
        elif action == "notification-open":
            services.app_action(app, "open-recording")
        elif action == "shortcut":
            services.activate()
        elif action == "record":
            app.recording_settings.set_boolean("skip-record-first", True)
            app.activate_action("record", None)
        elif action == "stop":
            assert window is not None
            meeting = next(m for m in window.snapshot.meetings if str(m.directory) == packet["path"])
            window.open_meeting(meeting)
            assert window.recording_page.stop.get_sensitive()
            window.recording_page.stop.emit("clicked")
        elif action == "selection":
            assert window is not None
            meeting = next(m for m in window.snapshot.meetings if str(m.directory) == packet["path"])
            window.open_meeting(meeting)
            page = window.recording_page
            page.change_sources.emit("clicked")
            page.sources_dialog.radios[(packet["me"], packet["others"])].set_active(True)
        elif action == "sources-done":
            assert window and window.recording_page and window.recording_page.sources_dialog
            window.recording_page.sources_dialog.done.emit("clicked")
        elif action == "stop-all":
            assert window is not None
            window._stop_live()
        elif action == "show-settings":
            app.on_preferences()
        elif action == "auto":
            dialog = app.settings_dialog
            assert dialog is not None and not dialog.closed and dialog._future is None
            dialog.automatic.set_active(packet["enabled"])
        elif action == "close-window":
            assert window is not None
            if app.settings_dialog and not app.settings_dialog.closed:
                app.settings_dialog.force_close()
            window.close()
            dialog = app._capture_confirmation
            if dialog:
                dialog.emit("response", "keep")
                dialog.force_close()
        window = next((w for w in app.get_windows() if isinstance(w, LibraryWindow)), None)
        result.update(
            status="Library" if window and window.get_visible() else "Window closed",
            title=window.get_title() if window else None,
            connected=app._browser_snapshot.get("connectedBrowsers", 0),
            auto=app._browser_snapshot.get("autoRecord", False),
            meetings=app._browser_snapshot.get("meetings", []),
            statuses=app._browser_snapshot.get("statuses", []),
            recordReady=not (
                app._record_start_future
                or app._browser_stops
                or app.browser_capture_active()
                or any(recorder_busy(r) for r in app.native_captures.values())
            ),
            recordStartPending=app._record_start_future is not None,
            browserStopsPending=bool(app._browser_stops),
            shortcutBound=bool(app.global_shortcut and app.global_shortcut.bound),
            settingsReady=bool(
                app.settings_dialog
                and not app.settings_dialog.closed
                and app.settings_dialog._future is None
                # A cached status can finish before Watch's newer snapshot.
                # Do not toggle an already-false row while the broker is true.
                and app.settings_dialog.automatic.get_active() == app._browser_snapshot.get("autoRecord", False)
            ),
            settingsAuto=app.settings_dialog.automatic.get_active() if app.settings_dialog else None,
            settingsSnapshotAuto=app.settings_dialog._snapshot.get("autoRecord") if app.settings_dialog else None,
            settingsSnapshotRevision=app.settings_dialog._snapshot.get("autoRecordRevision")
            if app.settings_dialog
            else None,
            autoRecordRevision=app._auto_recording_revision,
            automaticChangePending=app._auto_recording_pending,
            sourceChangePending=bool(app._source_changes),
            notifications=services.notifications,
            library=[
                dict(
                    path=str(m.directory),
                    title=m.title,
                    lifecycle=m.lifecycle,
                    interruptedAtMs=m.interrupted_at_ms,
                    header=window.capture_header.get_visible_child_name(),
                )
                for m in window.snapshot.meetings
            ]
            if window and window.snapshot
            else [],
            error=app.recording_error,
        )
    except Exception as error:
        result["error"] = str(error)
    finally:
        ready.set()
    return GLib.SOURCE_REMOVE


def requests():
    for line in sys.stdin:
        result, ready = {}, threading.Event()
        packet = {}
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


threading.Thread(target=requests, daemon=True).start()
sys.exit(app.run([]))
