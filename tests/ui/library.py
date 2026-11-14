#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private-display integration: production model, widgets and application actions."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")

ROOT = Path(__file__).resolve().parents[2]
build = Path(os.environ["CASSINI_TEST_BUILD"]).resolve()
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import Adw, Gio, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-talk.gresource")))
from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.player import Player  # noqa: E402
from cassini_talk.strings import text  # noqa: E402


def until(check):
    dispatch_until(check, "Library GUI did not complete")


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
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schema_dir / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schema_dir)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schema_dir)
    app = CassiniApplication("com.gocassini.LibraryTest", "test", "development")
    app.register(None)
    assert app._watchdog is not None and app._watchdog._thread is None
    assert not app._startup_finished
    for model in (app.speech_models, app.diarization_models, app.voice_models):
        if model is not None:
            assert not model._inspect_started
    app.activate()
    window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not window._pending and app._startup_finished)
    assert app._watchdog._thread is not None and app._watchdog._thread.is_alive()
    for model in (app.speech_models, app.diarization_models, app.voice_models):
        if model is not None:
            until(lambda model=model: not model.snapshot()["busy"])
    assert window.get_title() == text("app.name") == "Cassini talk"
    assert len(window.snapshot.meetings) == 9
    assert window.selected.title == "Budget review"
    assert window.selected.words and window.meeting_page is not None
    assert window.meeting_page.meeting.key == window.selected.key, "Folders mount the Meeting view in the right pane"
    assert window.root_box.get_visible_child_name() == "library"
    assert window.capture_header.get_visible_child_name() == "live"
    assert window.live_timer.get_label() == "12:48" and not window.live_stop.get_sensitive()
    from painted import painted_node
    from PIL import Image

    from cassini_talk.tokens import token

    until(lambda: window.live_stop.get_width() > 20)
    node = painted_node(window)
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    shot_path = Path(tmp) / "library-live-stop.png"
    assert texture.save_to_png(str(shot_path))
    ok, stop_bounds = window.live_stop.compute_bounds(window)
    assert ok
    stop_rgb = (
        Image.open(shot_path)
        .convert("RGB")
        .getpixel((int(stop_bounds.get_x() + 10), int(stop_bounds.get_y() + stop_bounds.get_height() / 2)))
    )
    record_hex = token("record", dark=False).lstrip("#")
    expected_rgb = tuple(int(record_hex[i : i + 2], 16) for i in (0, 2, 4))
    assert all(abs(a - b) <= 6 for a, b in zip(stop_rgb, expected_rgb, strict=True)), (
        f"Static live Stop pill washed out: got {stop_rgb}, expected {expected_rgb}"
    )
    labels = [w.get_label() for w in walk(window.rows) if isinstance(w, Gtk.Label)]
    assert "Listening inside your browser" in labels
    assert "47 min" in labels
    assert "47 min · the audio is ready to play" not in labels
    assert not any(value.startswith("“") and value.endswith("”") for value in labels), "L-53 removes row quote lines"
    assert not any(value.startswith("47 min · Marta") for value in labels)
    assert "·" in labels
    assert "Marta, Dario, Speaker 3" in labels
    assert window.search_entry.get_mapped() and window.search_entry.get_placeholder_text() == text(
        "search.field.placeholder"
    )
    assert window.import_audio_button.get_mapped() and window.import_audio_button.get_tooltip_text() == text(
        "import.action"
    )
    assert window.drop_footer.get_mapped() and window.drop_footer_label.get_label() == text("import.drop.rest")
    assert not any(
        isinstance(w, Gtk.Button) and w.get_action_name() == "win.search" for w in walk(window.sidebar_header)
    ), "L-21 removes sidebar search toggle button"
    assert not any(value.startswith("colour-") for w in walk(window.meeting_page) for value in w.get_css_classes())
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
    assert "Marta" in [w.get_label() for w in walk(window.meeting_page.speakers) if isinstance(w, Gtk.Label)]
    auto = [w for w in walk(window.meeting_page.speakers) if isinstance(w, Gtk.Label) and w.get_label() == "auto"]
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
    primary_actions = set(menu_actions(primary.get_menu_model()))
    assert "app.open" in primary_actions and "win.export" not in primary_actions, (
        "L-23/L-25: Import audio… stays in ☰ while per-meeting Export… moves to the meeting header bar"
    )
    primary.popup()
    until(primary.get_popover().get_mapped)
    item = next(
        w
        for w in walk(primary.get_popover())
        if w.__gtype__.name == "GtkModelButton"
        and any(isinstance(child, Gtk.Label) and child.get_text() == "Keyboard shortcuts" for child in walk(w))
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
    detail = window.meeting_page
    first_row.grab_focus()
    focus = window.get_focus()
    for _ in range(3):
        window._updated(window.snapshot)
        window._update_live()
    assert window.rows.get_selected_row() is first_row
    assert window.meeting_page is detail
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
    window.rows.select_row(None)
    assert window.meeting_page is None and not window.player.playing
    assert window.detail_stack.get_visible_child_name() == "empty"
    assert window.empty_selection_label.get_label() == text("meeting.emptySelection")
    window.rows.select_row(received)
    assert window.meeting_page is not None and not window.player.playing
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
    assert text("firstRun.hero.title") in labels and text("firstRun.hero.subtitle") in labels
    assert text("firstRun.body") in labels and text("firstRun.footer") in labels
    assert (
        text("firstRun.title") not in labels
        and text("firstRun.shortcutHint", shortcut=text("common.shortcut.record")) not in labels
    )
    assert empty.privacy_footer.get_visible()
    # L-27: no header Record, no visible header title, only a hidden accessible "Library" heading.
    empty_view = empty.root_box.get_child_by_name("empty")
    headers = [w for w in walk(empty_view) if isinstance(w, Adw.HeaderBar)]
    assert len(headers) == 1
    header_buttons = [w for w in walk(headers[0]) if isinstance(w, Gtk.Button) and w.get_action_name() == "app.record"]
    assert header_buttons == [], "first-run header must not carry Record"
    assert not any(isinstance(w, Gtk.Label) and w.get_label() == text("app.name") for w in walk(headers[0]))
    assert empty.first_run_heading.get_label() == text("library.title")
    assert empty.first_run_heading.get_accessible_role() == Gtk.AccessibleRole.HEADING
    assert empty.first_run_heading.get_visible() and empty.first_run_heading.get_opacity() == 0.0
    # Big Record pill sits at the start of the wave, in the hero block.
    from cassini_talk.brand_mark import NAV_MARK_RESOURCE, BrandMark
    from cassini_talk.first_run_hero import HeroWave

    wave_row = empty.hero_wave.get_parent()
    record = wave_row.get_first_child()
    assert record.get_action_name() == "app.record" and "big-record" in record.get_css_classes()
    assert record.get_next_sibling() is empty.hero_wave and isinstance(empty.hero_wave, HeroWave)
    marks = [w for w in walk(empty_view) if isinstance(w, BrandMark)]
    assert len(marks) == 1 and marks[0].resource_path == NAV_MARK_RESOURCE and marks[0].height_px == 40
    assert empty.hero_title.get_label() == text("firstRun.hero.title")
    assert "hero-subtitle" in empty.hero_subtitle.get_css_classes()
    # D-972: the 720 px column is a cap, not a minimum. A 640 px window stays 640 px wide
    # and keeps the header menu, Close and the hero Record pill on screen.
    empty.set_default_size(640, 720)
    minimum = empty.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
    assert minimum <= 640, f"first-run window needs {minimum} px; it must fit 640 px"
    # get_width() excludes the window frame, so it reads slightly under the requested 640.
    until(lambda: 0 < empty.get_width() <= 640)
    menu = next(w for w in walk(headers[0]) if isinstance(w, Gtk.MenuButton))
    controls = [w for w in walk(headers[0]) if isinstance(w, Gtk.WindowControls) and not w.get_empty()]
    close = next(w for c in controls for w in walk(c) if isinstance(w, Gtk.Button) and "close" in w.get_css_classes())
    for widget, name in ((menu, "menu"), (close, "Close"), (record, "Record pill"), (empty.hero_wave, "wave")):
        ok, bounds = widget.compute_bounds(empty)
        assert ok and widget.get_mapped(), name
        assert bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= empty.get_width(), (name, bounds.get_x())
    empty.set_default_size(1120, 720)
    until(lambda: empty.get_width() > 1000)
    clamp = next(w for w in walk(empty_view) if isinstance(w, Adw.Clamp))
    until(lambda: clamp.get_child().get_width() == 720)  # wide windows keep the 720 px column
    # A sandbox fallback is the initial Library too, but its footer must not
    # claim Music/Cassini. Settings/Show in Files use the actual primary path.
    from cassini_talk import library_location

    fallback = Path(tmp) / "private-data/cassini/Library"
    with (
        patch.dict(os.environ),
        patch.object(library_location, "initial_root", return_value=fallback),
        patch.object(library_location, "primary", return_value=fallback),
    ):
        os.environ.pop("CASSINI_LIBRARY_DIR", None)
        empty._updated(empty.snapshot)
        assert not empty.privacy_footer.get_visible()
    empty._updated(empty.snapshot)
    assert empty.privacy_footer.get_visible()
    app.on_about()
    until(lambda: isinstance(empty.get_visible_dialog(), Adw.AboutDialog))
    about = empty.get_visible_dialog()
    assert about.get_application_name() == "Cassini talk"
    assert about.get_application_icon() == "saturn-mid"
    about.close()
    until(lambda: empty.get_visible_dialog() is None)
    app.activate_action("record", None)
    assert app.record_first is not None and app.recorder is None, "Opening Record must not start devices"
    app.record_first.close()
    empty.close()
print("Library GUI passed: fixtures, selection, singleton, menu, first-run and Record routing")
