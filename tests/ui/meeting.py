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
    # Growing transcripts splice stable rows and never drag a paused reader back.
    from dataclasses import replace

    from cassini_viewer.meeting_text import DARK_COLOURS, reading_turns
    from cassini_viewer.portable import Word

    timed = tuple(
        Word(meeting.speakers[i % len(meeting.speakers)].id, i * 1000, i * 1000 + 900, f"Item {i}") for i in range(400)
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
    until(lambda: window.meeting_page is not None)
    assert window.meeting_page.meeting.words
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
