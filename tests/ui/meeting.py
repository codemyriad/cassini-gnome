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
from unittest.mock import patch

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
sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402

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


services = DesktopServices()
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
    assert all(v in labels(page.speakers) for v in ["43%", "36%", "21%", "Named by you", "Not named yet"])
    # Imported recognition metadata has no corresponding local saved voice.
    assert "Recognised from saved voices" not in labels(page.speakers)
    menu_changes = []
    handler = page.more_menu.connect("items-changed", lambda *_: menu_changes.append(1))
    page.update(meeting)
    page.update(meeting)
    assert not menu_changes, "An unchanged meeting must not rebuild the More menu"
    page.more_menu.disconnect(handler)
    window.player.seek(192000)
    current = next(row for row in page.bound.values() if row[0].speaker == "guest")
    assert "background=" in current[1].get_label()
    assert 'alpha="60%"' in page.bound[3][1].get_label()
    assert page.elapsed.get_label() == "00:03:12" and page.total.get_label() == "00:47:05"
    assert isinstance(page.rate_button, Gtk.MenuButton)
    window.activate_action("win.rate", GLib.Variant("s", "1.5"))
    assert window.player.rate == 1.5 and page.rate_button.get_label() == "1.5×"
    assert window.lookup_action("rate").get_state().get_string() == "1.5"
    window.activate_action("win.rate", GLib.Variant("s", "nan"))
    assert window.player.rate == 1.5, "Invalid rate must be ignored"
    # Real GStreamer clock, synchronised fakesink: 4 wall seconds at 2×.
    window.activate_action("win.rate", GLib.Variant("s", "2"))
    window.player.seek(10000)
    until(lambda: window.player._prerolled)
    window.player.play()
    until(lambda: window.player.position > 10100)
    started, position = time.monotonic(), window.player.position
    until(lambda: time.monotonic() - started >= 4)
    advance = window.player.position - position
    window.player.pause()
    assert 7000 <= advance <= 9000, ("2× clock advance", advance)
    print(f"PASS real 2× playback: {advance} ms in 4 wall seconds", flush=True)
    window.activate_action("win.rate", GLib.Variant("s", "1.5"))
    window.player.seek(192000)

    current[1].emit("activate-link", "item:0")
    until(lambda: window.player.position == 188000)
    page.seek.emit("change-value", Gtk.ScrollType.JUMP, 190000)
    assert window.player.position == 190000
    page.play.emit("clicked")
    until(lambda: window.player.playing)
    assert page.play.get_tooltip_text() == "Pause"
    page.play.emit("clicked")
    assert not window.player.playing
    # D26: both surfaces explain a completed empty transcript and run the
    # normal write-down action. No real model, device or user Library is used.
    with patch.object(window.monitor, "callback", lambda _snapshot: None):
        from dataclasses import replace
        from types import SimpleNamespace

        # Queued/running meetings must explain the wait even before the first
        # words arrive, and keep showing it alongside a growing transcript.
        queued = replace(meeting, words=(), turns=(), preview_turns=(), transcript="queued", eta_minutes=None)
        page.update(queued)
        assert "Transcribing…" in labels(page.lines), "Queued without words must show progress immediately"
        assert not page.write_button.get_visible()
        running = replace(meeting, transcript="running", eta_minutes=3)
        page.update(running)
        assert "Transcribing, about 3 minutes left" in labels(page.lines)
        assert page._turns == running.turns, "Progress must not hide already written words"
        page.update(replace(running, eta_minutes=0))
        assert "Transcribing, less than a minute left" in labels(page.lines), "ETA-only refresh must update"
        page.update(replace(meeting, transcript="done", eta_minutes=None))
        assert not any(value.startswith("Transcribing") for value in labels(page.lines))

        empty = replace(meeting, words=(), turns=(), preview_turns=(), transcript="done", eta_minutes=None)
        assert empty.no_speech and empty.can_write_down

        def widgets(widget):
            yield widget
            child = widget.get_first_child()
            while child:
                yield from widgets(child)
                child = child.get_next_sibling()

        with patch.object(app, "write_down") as retry:
            page.update(empty)
            assert "No speech was found in this meeting." in labels(page.lines)
            again = next(w for w in widgets(page.lines) if isinstance(w, Gtk.Button))
            assert again.get_label() == "Write it down again"
            again.emit("clicked")
            retry.assert_called_once_with(empty)
            retry.reset_mock()
            window._selected(None, SimpleNamespace(meeting=empty))
            assert "No speech was found in this meeting." in labels(window.preview)
            again = next(
                w
                for w in widgets(window.preview)
                if isinstance(w, Gtk.Button) and w.get_label() == "Write it down again"
            )
            again.emit("clicked")
            retry.assert_called_once_with(empty)
        page.update(meeting)
        window._selected(None, SimpleNamespace(meeting=meeting))
    # Synthetic growth owns its model until it is restored. Live Library
    # notifications must not replace it with the on-disk fixture mid-assertion.
    with patch.object(window.monitor, "callback", lambda _snapshot: None):
        # Growing transcripts splice stable rows and never drag a paused reader back.
        from dataclasses import replace

        from cassini_viewer.meeting_text import DARK_COLOURS, reading_turns
        from cassini_viewer.portable import Word

        timed = tuple(
            Word(meeting.speakers[i % len(meeting.speakers)].id, i * 1000, i * 1000 + 900, f"Item {i}")
            for i in range(400)
        )
        page.update(replace(meeting, words=timed, turns=reading_turns(timed)))
        until(lambda: page.scroller.get_vadjustment().get_upper() > 5000)
        adjustment = page.scroller.get_vadjustment()
        adjustment.set_value(1500)
        until(lambda: adjustment.get_value() > 1000)
        frames = []
        page.list.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 2)[1])
        until(lambda: len(frames) >= 2)

        def visible_anchor():
            candidates = []
            for turn, _words, row in page.bound.values():
                success, bounds = row.compute_bounds(page.scroller)
                if success and bounds.get_y() + bounds.get_height() > 0 and bounds.get_y() < page.scroller.get_height():
                    candidates.append((turn.index, bounds.get_y()))
            return min(candidates)

        stable = page.store.get_item(1)
        anchor = visible_anchor()
        grown = timed + (Word(meeting.speakers[0].id, 400000, 400900, "New words"),)
        page.update(replace(meeting, words=grown, turns=reading_turns(grown)))
        frames = []
        page.list.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 2)[1])
        until(lambda: len(frames) >= 2)
        assert page.store.get_item(1) is stable
        after = visible_anchor()
        assert after[0] == anchor[0] and abs(after[1] - anchor[1]) < 2, (after, anchor)
        # Resuming after reading elsewhere must return to the active turn, even
        # when a paused model update already computed that same timed item.
        page.play.emit("clicked")

        def active_visible():
            for turn, _words, row in page.bound.values():
                if turn.index == page._reading_index:
                    success, bounds = row.compute_bounds(page.scroller)
                    return success and 0 <= bounds.get_y() < page.scroller.get_height()
            return False

        until(lambda: window.player.playing and active_visible())
        page.play.emit("clicked")
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        until(lambda: page._theme is True)
        assert page._colour(meeting.speakers[0]) == DARK_COLOURS[0]
        assert any(DARK_COLOURS[0] in value for value in labels(page.speakers))
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        until(lambda: page._theme is False)
    page.update(meeting)
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
    until(lambda: not window.meeting_page and window.selected and window.selected.key == received.key)
    window.read_button.emit("clicked")
    until(lambda: window.meeting_page is not None)
    assert window.meeting_page.meeting.words
    assert not isinstance(window.player, FolderPlayer), "Received meeting must exercise player replacement"
    assert window.player.rate == 1.5 and window.meeting_page.rate_button.get_label() == "1.5×"
    subprocess.run(["xdotool", "key", "ctrl+bracketright"], check=True)
    until(lambda: window.player.rate == 1.75)
    assert window.meeting_page.rate_button.get_label() == "1.75×"
    app.on_shortcuts()
    until(lambda: app.shortcuts_dialog and app.shortcuts_dialog.get_mapped())
    shortcut_labels = labels(app.shortcuts_dialog)
    assert "Increase playback speed" in shortcut_labels and "Decrease playback speed" in shortcut_labels
    app.shortcuts_dialog.close()
    until(lambda: app.shortcuts_dialog is None)

    assert len(app.get_windows()) == 1, "File opens belong to the pushed reading page"
    # Library files opened via Open/CLI remain attached to live model updates.
    own = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    app.open_in_new_window(Gio.File.new_for_path(str(own.directory / "session.json")))
    until(lambda: window.meeting_page and window.meeting_page.meeting.key == own.key)
    assert not window._external_meeting
    changed = json.loads((own.directory / "meeting.json").read_text())
    changed.update(title="Live Library rename", titleSource="user")
    (own.directory / "meeting.json").write_text(json.dumps(changed))
    until(lambda: window.meeting_page.meeting.title == "Live Library rename")
    # An outside folder is read-only; the underlying Library keeps refreshing.
    from cassini_viewer.recording_session import RecordingSession

    outside = Path(temporary) / "outside"
    outside.mkdir()
    session = RecordingSession(outside)
    session.add_track("local", "Mic", "local", 48000, 1)
    session.append("local", bytes(9600), 0)
    session.finish()
    app.open_in_new_window(Gio.File.new_for_path(str(session.directory / "session.json")))
    until(lambda: window.meeting_page and window.meeting_page.meeting.directory == session.directory)
    assert window._external_meeting and not window.meeting_page.meeting.metadata_editable
    assert not window.meeting_page.write_button.get_visible()
    changed["title"] = "Updated underneath external reading"
    (own.directory / "meeting.json").write_text(json.dumps(changed))
    until(lambda: any(m.title == changed["title"] for m in window.snapshot.meetings))
    assert window.meeting_page.meeting.directory == session.directory
    window.back_to_library()
    assert any(m.title == changed["title"] for m in window.snapshot.meetings)
    # Real confirmation widgets, deterministic controller double; no device access.
    recording = Adw.ApplicationWindow(application=app)

    class Controller(GObject.Object):
        __gsignals__ = {name: (GObject.SignalFlags.RUN_LAST, None, (str,)) for name in ("finished", "error")}
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
    dialog = app._capture_confirmation
    dialog.emit("response", "keep")
    dialog.force_close()
    until(lambda: not window.get_visible())
    assert recorder.pipeline is not None and app._capture_background
    app.activate()
    assert window.get_visible()
    app.on_quit()
    dialog = app._capture_confirmation
    dialog.emit("response", "stop")
    dialog.force_close()
    try:
        until(lambda: not app.get_windows())
    except AssertionError:
        print([(type(w).__name__, w.get_visible()) for w in app.get_windows()], flush=True)
        raise
print(
    "Meeting GUI passed: timed reading, folder/Opus navigation, playback, card persistence, live updates and close confirmation"
)
