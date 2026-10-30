#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private-display integration: production model, widgets and application actions."""

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
build = Path(os.environ["CASSINI_TEST_BUILD"]).resolve()
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-viewer.gresource")))
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.player import Player  # noqa: E402
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


def menu_actions(model):
    for index in range(model.get_n_items()):
        action = model.get_item_attribute_value(index, "action", None)
        if action:
            yield action.get_string()
        for link in ("section", "submenu"):
            child = model.get_item_link(index, link)
            if child:
                yield from menu_actions(child)


with tempfile.TemporaryDirectory() as tmp:
    fixtures = Path(tmp) / "fixtures"
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(fixtures)], check=True)
    os.environ["CASSINI_LIBRARY_DIR"] = str(fixtures / "main/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(fixtures / "main/activity.json")
    os.environ["CASSINI_SCREENSHOT_NOW"] = "1793107080000"
    schema_dir = Path(tmp) / "schemas"
    schema_dir.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schema_dir / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schema_dir)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schema_dir)
    app = CassiniApplication("com.gocassini.LibraryTest", "test", "development")
    app.register(None)
    app.activate()
    window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not window._pending)
    assert len(window.snapshot.meetings) == 9
    assert window.selected.title == "Budget review"
    assert window.selected.words and window.read_button.get_sensitive()
    assert window.read_button.get_sensitive(), "Folders have a real Meeting page"
    assert window.root_box.get_visible_child_name() == "library"
    assert window.capture_header.get_visible_child_name() == "live"
    assert window.live_timer.get_label() == "12:48" and not window.live_stop.get_sensitive()
    labels = [w.get_label() for w in walk(window.rows) if isinstance(w, Gtk.Label)]
    assert "Listening inside your browser" in labels
    assert "47 min · the audio is ready to play" in labels
    assert not any(value.startswith("47 min · Marta") for value in labels)
    assert not any(value.startswith("colour-") for w in walk(window.preview) for value in w.get_css_classes())
    assert window.play_button.get_sensitive(), "Saved folders with known offsets can play"
    window.player.shutdown()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player = FolderPlayer(audio_sink=sink)
    window._connect_player()
    window.play_button.emit("clicked")
    until(lambda: window.player.playing and window.player.position > 0)
    assert window.player.duration == 3120000
    window.activate_action("win.play-pause", None)
    assert not window.player.playing
    window.play_button.emit("clicked")
    until(lambda: window.player.playing)
    assert "Marta" in [w.get_label() for w in walk(window.preview) if isinstance(w, Gtk.Label)]
    auto = [w for w in walk(window.preview) if isinstance(w, Gtk.Label) and w.get_label() == "auto"]
    assert not auto  # an imported recognised name has no saved local voice
    app.activate()
    assert sum(isinstance(w, LibraryWindow) for w in app.get_windows()) == 1
    assert app.lookup_action("new-window") is None
    assert app.lookup_action("browser-meetings") is None
    assert app.get_accels_for_action("app.browser-meetings") == []
    menus = [w.get_menu_model() for w in walk(window) if isinstance(w, Gtk.MenuButton)]
    assert all("app.browser-meetings" not in set(menu_actions(m)) for m in menus if m)
    primary = next(
        w
        for w in walk(window)
        if isinstance(w, Gtk.MenuButton)
        and w.get_mapped()
        and w.get_menu_model()
        and "app.shortcuts" in set(menu_actions(w.get_menu_model()))
    )
    primary.popup()
    until(primary.get_popover().get_mapped)
    item = next(
        w
        for w in walk(primary.get_popover())
        if w.__gtype__.name == "GtkModelButton"
        and any(isinstance(child, Gtk.Label) and child.get_text() == "Keyboard Shortcuts" for child in walk(w))
    )
    assert item.is_sensitive() and item.activate(), "Activate through the actual primary-menu item"
    until(lambda: app.shortcuts_dialog is not None and app.shortcuts_dialog.get_mapped())
    shortcuts = app.shortcuts_dialog
    assert app.get_accels_for_action("app.shortcuts") == ["<Control>question"]
    app.activate_action("shortcuts", None)
    assert app.shortcuts_dialog is shortcuts, "Repeated invocation must not stack dialogs"
    shortcut_labels = [w.get_text() for w in walk(shortcuts) if isinstance(w, Gtk.Label)]
    assert "Back 10 seconds" in shortcut_labels and "Forward 10 seconds" in shortcut_labels
    assert "Ctrl+Shift+E" in shortcut_labels or "Ctrl" in shortcut_labels
    shortcuts.close()
    until(lambda: app.shortcuts_dialog is None)
    # Idle/live-clock ticks and equivalent snapshots retain widget identity,
    # selection, focus and scroll rather than tearing down the Library.
    first_row = window.rows.get_selected_row()
    preview = window.preview.get_first_child()
    first_row.grab_focus()
    focus = window.get_focus()
    for _ in range(3):
        window._updated(window.snapshot)
        window._update_live()
    assert window.rows.get_selected_row() is first_row
    assert window.preview.get_first_child() is preview
    assert window.get_focus() is focus
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
    # Decode the real received Opus into a fakesink, with no audio device access.
    window.player.shutdown()
    window.player = Player()
    window._connect_player()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player._pipeline.set_property("audio-sink", sink)
    window.play_button.emit("clicked")
    until(lambda: window.player.playing and window.player.position > 0)
    assert window.player.duration == 2040000
    other = Adw.ApplicationWindow(application=app)
    other.player = type(window.player)()
    app.watch_player(other.player)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    other.player._pipeline.set_property("audio-sink", sink)
    other.player.load(window.selected.audio_file.as_uri(), window.selected.duration_ms)
    other.player.play()
    until(lambda: other.player.playing and not window.player.playing)
    window.play_button.emit("clicked")
    until(lambda: window.player.playing and not other.player.playing)
    other.player.shutdown()
    other.close()
    assert window.read_button.get_sensitive()
    window.read_button.emit("clicked")
    assert window.meeting_page and not window.player.playing
    window.back_to_library()
    until(lambda: window.meeting_page is None)
    window.play_button.emit("clicked")
    until(lambda: window.player.playing)
    # #8's load() can reject synchronously; the Library must honour that error.
    find = Gst.ElementFactory.find
    Gst.ElementFactory.find = lambda name: None if name == "opusdec" else find(name)
    try:
        window._loaded_key = None
        window.play_button.emit("clicked")
        assert not window.player.playing, "A rejected load must not play the previous URI"
        assert not window.play_button.get_sensitive()
    finally:
        Gst.ElementFactory.find = find
    value = json.loads((window.selected.directory / "meeting.json").read_text())
    value["title"] = "Received title changed after playback error"
    (window.selected.directory / "meeting.json").write_text(json.dumps(value))
    until(lambda: window.selected.title == value["title"])
    assert not window.play_button.get_sensitive(), "A monitor refresh must preserve playback failure state"
    received = next(w for w in walk(window.rows) if hasattr(w, "meeting") and w.meeting.origin == "received")
    window.rows.select_row(window.rows.get_row_at_index(1))
    window.rows.select_row(received)
    window.play_button.emit("clicked")
    until(lambda: window.player.playing and window.player.position > 0)
    audio_path = window.selected.audio_file
    original_audio = audio_path.read_bytes()
    audio_path.unlink()
    until(lambda: not window.selected.has_audio)
    assert not window.player.playing and not window.play_button.get_sensitive()
    audio_path.write_bytes(original_audio)
    until(lambda: window.selected.has_audio)
    window.close()
    assert not window.player.playing
    os.environ["CASSINI_LIBRARY_DIR"] = str(fixtures / "received-only/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(fixtures / "received-only/activity.json")
    app.activate()
    single = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not single._pending)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    single.player._pipeline.set_property("audio-sink", sink)
    single.play_button.emit("clicked")
    until(lambda: single.player.playing)
    shutil.rmtree(single.selected.directory)
    until(lambda: single.root_box.get_visible_child_name() == "empty")
    assert not single.player.playing and single._preview_key is None
    single.close()
    offline_root = Path(tmp) / "mounted-library"
    offline_root.mkdir()
    source = next((fixtures / "main/root").glob("Received-*"))
    shutil.copytree(source, offline_root / source.name)
    link = Path(tmp) / "library-link"
    link.symlink_to(offline_root, target_is_directory=True)
    os.environ["CASSINI_LIBRARY_DIR"] = str(link)
    app.activate()
    offline = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not offline._pending)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    offline.player._pipeline.set_property("audio-sink", sink)
    offline.play_button.emit("clicked")
    until(lambda: offline.player.playing)
    offline_root.rename(offline_root.with_name("disconnected-library"))
    until(lambda: offline.root_box.get_visible_child_name() == "offline")
    assert not offline.player.playing and offline._preview_key is None
    offline.close()
    os.environ["CASSINI_LIBRARY_DIR"] = str(fixtures / "empty/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(fixtures / "empty/activity.json")
    app.activate()
    empty = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not empty._pending)
    assert empty.root_box.get_visible_child_name() == "empty"
    labels = [w.get_label() for w in walk(empty) if isinstance(w, Gtk.Label)]
    assert text("firstRun.title") in labels and text("firstRun.footer") in labels
    app.activate_action("record", None)
    assert app.record_first is not None and app.recorder is None, "Opening Record must not start devices"
    app.record_first.close()
    empty.close()
print("Library GUI passed: fixtures, selection, singleton, menu, first-run and Record routing")
