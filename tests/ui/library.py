#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private-display integration: production model, widgets and application actions."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")

ROOT = Path(__file__).resolve().parents[2]
build = Path(os.environ["CASSINI_TEST_BUILD"]).resolve()
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-viewer.gresource")))
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.recording_window import RecordingWindow  # noqa: E402
from cassini_viewer.strings import text  # noqa: E402


def until(predicate):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("GUI condition timed out")


def walk(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from walk(child)
        child = child.get_next_sibling()


with tempfile.TemporaryDirectory() as tmp:
    fixtures = Path(tmp) / "fixtures"
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(fixtures)], check=True)
    os.environ["CASSINI_LIBRARY_DIR"] = str(fixtures / "main/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(fixtures / "main/activity.json")
    os.environ["CASSINI_SCREENSHOT_NOW"] = "1793107080000"
    app = CassiniApplication("com.gocassini.LibraryTest", "test", "development")
    app.register(None)
    app.activate()
    window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not window._pending)
    assert len(window.snapshot.meetings) == 9
    assert window.selected.title == "Budget review"
    assert window.root_box.get_visible_child_name() == "library"
    assert not window.play_button.get_sensitive(), "Folder playback waits for UX-play"
    assert "Marta" in [w.get_label() for w in walk(window.preview) if isinstance(w, Gtk.Label)]
    auto = [w for w in walk(window.preview) if isinstance(w, Gtk.Label) and w.get_label() == "auto"]
    assert auto and auto[0].get_tooltip_text() == text("common.autoTag.tooltip")
    app.activate()
    assert sum(isinstance(w, LibraryWindow) for w in app.get_windows()) == 1
    assert app.lookup_action("new-window") is None
    assert app.get_accels_for_action("app.browser-meetings") == []
    menus = [w.get_menu_model() for w in walk(window) if isinstance(w, Gtk.MenuButton)]
    assert any(
        m.get_item_attribute_value(0, "label", None).get_string() == text("common.legacy.browserMeetings")
        for m in menus
        if m
    )
    selected_dir = window.selected.directory
    metadata = selected_dir / "meeting.json"
    value = json.loads(metadata.read_text())
    value["title"] = "Budget review updated"
    temporary = metadata.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(metadata)
    until(lambda: window.selected is not None and window.selected.title == "Budget review updated")
    assert window.selected.directory == selected_dir, "Monitor updates must preserve selection"
    received = next(w for w in walk(window.rows) if hasattr(w, "meeting") and w.meeting.origin == "received")
    window.rows.select_row(received)
    assert window.selected.origin == "received" and window.play_button.get_sensitive()
    window.close()
    os.environ["CASSINI_LIBRARY_DIR"] = str(fixtures / "empty/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(fixtures / "empty/activity.json")
    app.activate()
    empty = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not empty._pending)
    assert empty.root_box.get_visible_child_name() == "empty"
    labels = [w.get_label() for w in walk(empty) if isinstance(w, Gtk.Label)]
    assert text("firstRun.title") in labels and text("firstRun.footer") in labels
    app.activate_action("record", None)
    recording = next(w for w in app.get_windows() if isinstance(w, RecordingWindow))
    assert recording.recorder.pipeline is None, "Opening Record must not start devices"
    recording.close()
    empty.close()
print("Library GUI passed: fixtures, selection, singleton, menu, first-run and Record routing")
