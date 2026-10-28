#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Search menus, phrase matching, context playback, refresh and Meeting navigation."""

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
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.player import Player  # noqa: E402


def until(check):
    end = time.monotonic() + 12
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Search GUI condition timed out"


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(tmp / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(tmp / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    connect = LibraryWindow._connect_player

    def private_connect(window):
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", True)
        if isinstance(window.player, FolderPlayer):
            window.player.audio_sink = sink
        elif isinstance(window.player, Player):
            window.player._pipeline.set_property("audio-sink", sink)
        connect(window)

    LibraryWindow._connect_player = private_connect
    app = CassiniApplication("com.gocassini.SearchTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    window.activate_action("win.search", None)
    page = window.search_page
    until(lambda: not page.pending)
    assert page.entry.has_focus() or window._focused_editors
    assert page.speaker_button.get_label() == "Speaker"
    assert page.date_button.get_label() == "Any date"
    assert not page.hits
    assert "Has summary" not in labels(page)
    page.entry.set_text("budget")
    page.choose_speaker("Marta")
    until(lambda: not page.pending)
    assert page.count.get_text() == "4 places in 3 meetings"
    assert page.store.get_item(0) is page.store.get_item(0)
    assert [hit.position_ms for hit in page.hits] == [4000, 872000, 1870000, 527000]
    assert "Any speaker" in labels(page.speaker_button.get_popover())
    assert [s for s in labels(page.date_button.get_popover())] == ["Any date", "Today", "Past 7 days", "Past 30 days"]
    assert all(not s.startswith("Speaker ") for s in page.names)

    # Exercise actual native popover choices, not only public setter helpers.
    def find_today(widget):
        if isinstance(widget, Gtk.CheckButton) and widget.get_label() == "Today":
            return widget
        child = widget.get_first_child()
        while child:
            found = find_today(child)
            if found:
                return found
            child = child.get_next_sibling()
        return None

    today = find_today(page.date_button.get_popover())
    today.set_active(True)
    until(lambda: not page.pending)
    assert len(page.hits) == 2 and page.date == "today"
    page.own.set_active(True)
    until(lambda: not page.pending)
    assert len(page.hits) == 2
    page.choose_date("any")
    page.own.set_active(False)
    until(lambda: not page.pending)
    page.entry.set_text("budget OR impossible")
    until(lambda: page.pending)
    until(lambda: not page.pending)
    assert not page.hits and page.count.get_text() == "0 places in 0 meetings"
    page.entry.set_text("budget")
    until(lambda: page.pending)
    until(lambda: not page.pending)
    page.selection.set_selected(0)
    page.list.grab_focus()
    until(lambda: window.lookup_action("play-pause").get_enabled())
    window.lookup_action("play-pause").activate(None)
    until(lambda: window.player.playing)
    window.lookup_action("play-pause").activate(None)
    assert not window.player.playing
    page.selection.set_selected(1)
    assert page.hit.position_ms == 872000
    window.lookup_action("play-pause").activate(None)
    until(lambda: window.player.playing and window.player.position >= 872000)
    position = window.player.position
    previous_snapshot = window.snapshot
    window.monitor.refresh_now()
    until(lambda: window.snapshot is not previous_snapshot)
    until(lambda: not page.pending)
    assert window.player.playing and window.player.position >= position
    assert "Play from 00:14:32" in labels(page.context)
    page.open.emit("clicked")
    until(lambda: window.meeting_page is not None and window.player.position >= 872000)
    assert window.search_page is None
    assert window.meeting_page.meeting.title == "Budget review"
    window.back_to_library()
    window.toggle_search()
    fresh = window.search_page
    until(lambda: not fresh.pending)
    assert fresh.speaker is None and fresh.date == "any" and not fresh.own.get_active()
    assert not fresh.hits and fresh.entry.get_text() == ""
    fresh.entry.set_text("the budget")
    until(lambda: fresh.pending)
    until(lambda: not fresh.pending)
    assert fresh.hits
    window.back_to_library()
    window.close()
    app.do_shutdown()
    print("PASS: Search phrase/menus/AND/date/order/reset/seek/refresh/navigation in private Xvfb")
