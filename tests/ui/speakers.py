#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual private GTK name edits, source names, merge/Undo, failure and sample."""

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
from gi.repository import Adw, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import speaker_edits  # noqa: E402
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import read_metadata  # noqa: E402


def until(check):
    deadline = time.monotonic() + 8
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    assert check(), "Speaker GUI did not settle"


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK
        if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
        else Adw.ColorScheme.FORCE_LIGHT
    )
    app = CassiniApplication("com.gocassini.SpeakersTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    capture = {
        p.name: p.read_bytes() for p in meeting.directory.iterdir() if p.name in ("session.json", "transcript.json")
    }
    window.player.shutdown()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player = FolderPlayer(audio_sink=sink)
    window._connect_player()
    window.open_meeting(meeting)
    page = window.meeting_page
    until(lambda: window.player._prerolled)
    window.player.seek(192000)
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: not window.lookup_action("play-pause").get_enabled())
    assert dialog.heading.get_label() == "Speaker 3" and dialog.entry.get_text() == ""
    assert dialog.talk.get_label() == "Speaks for 11 s in this meeting"
    assert any("Remember this voice" in value for value in labels(dialog.get_child()))
    assert not dialog.remember.get_sensitive()  # no private print in this fixture

    assert dialog.merge_menu.get_n_items() == 2 and dialog.save.get_sensitive()
    for value in ("x" * 65, "x\u202ey"):
        dialog.entry.set_text(value)
        assert not dialog.save.get_sensitive()
    dialog.entry.set_text("  Giulia  ")
    assert dialog.save.get_sensitive()
    dialog.play.emit("clicked")
    until(lambda: dialog.sample_active and window.player.playing)
    assert window.player._range_end == 198000
    dialog.play.emit("clicked")
    assert not window.player.playing and window.player.position == 192000 and window.player._range_end is None
    from dataclasses import replace

    dialog.update(replace(page.meeting, has_audio=False))
    dialog._tick()
    assert not dialog.play.get_sensitive()
    dialog.play.emit("clicked")
    assert not window.player.playing
    dialog.update(page.meeting)
    dialog._tick()
    assert dialog.play.get_sensitive()
    # A real failed update stays open and retains the field for Try again.
    with patch.object(speaker_edits, "save_name", side_effect=OSError("fixture read-only volume")):
        dialog.save.emit("clicked")
        until(lambda: not dialog.busy)
        assert not dialog.closed and dialog.error.get_visible() and dialog.save.get_label() == "Try again"
        assert dialog.entry.get_text() == "  Giulia  "
    dialog.save.emit("clicked")
    until(lambda: dialog.closed and any(s.name == "Giulia" for s in page.meeting.speakers))
    assert next(s for s in page.meeting.speakers if s.id == "guest").provenance == "user"
    assert "Giulia" in labels(page.speakers)
    assert window.lookup_action("play-pause").get_enabled()
    # Name the speakers uses the last chosen voice; clearing is a real save.
    page.name_speakers_button.emit("clicked")
    dialog = page.speakers_dialog
    assert dialog.identifier == "guest" and dialog.entry.get_text() == "Giulia"
    dialog.entry.set_text("")
    dialog.save.emit("clicked")
    until(lambda: dialog.closed and next(s for s in page.meeting.speakers if s.id == "guest").name == "Speaker 3")
    # Merge menu invokes an action rather than changing the source transcript.
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    dialog.actions.lookup_action("merge").activate(GLib.Variant("s", "marta"))
    until(lambda: dialog.closed and len(page.meeting.speakers) == 2)
    assert window.lookup_action("undo-speaker-merge").get_enabled()
    assert read_metadata(meeting.directory)["speakerMerges"] == [{"from": "guest", "into": "marta"}]
    assert all(p.read_bytes() == capture[p.name] for p in meeting.directory.iterdir() if p.name in capture)
    window.lookup_action("undo-speaker-merge").activate(None)
    until(lambda: len(page.meeting.speakers) == 3 and not window.lookup_action("undo-speaker-merge").get_enabled())
    # Cancel writes nothing, including after an unsaved edit.
    before = (meeting.directory / "meeting.json").read_bytes()
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    dialog.entry.set_text("Not saved")
    dialog.cancel.emit("clicked")
    until(lambda: dialog.closed)
    assert (meeting.directory / "meeting.json").read_bytes() == before
    # A normal many-person meeting must keep its last speaker/action reachable.
    from cassini_viewer.library import LibrarySpeaker
    from cassini_viewer.meeting_text import reading_turns
    from cassini_viewer.portable import Word

    window.monitor.close()
    height = window.get_height()
    voices = tuple(LibrarySpeaker(f"voice-{i}", f"Voice {i + 1}", None, i * 1000, i) for i in range(20))
    words = tuple(Word(voice.id, i * 1000, i * 1000 + 100, "Spoken") for i, voice in enumerate(voices))
    page.update(replace(page.meeting, speakers=voices, words=words, turns=reading_turns(words)))
    # Outside folders are readable, but offer no name/edit controls.
    frames = []
    window.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 3)[1])
    window.queue_draw()
    until(lambda: len(frames) >= 3)
    visible, bounds = page.name_speakers_button.compute_bounds(window)
    adjustment = page.speakers_scroller.get_vadjustment()
    assert adjustment.get_upper() > adjustment.get_page_size()
    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

    def last_speaker_action_reachable():
        visible, bounds = page.name_speakers_button.compute_bounds(window)
        return visible and bounds.get_y() + bounds.get_height() <= window.get_height()

    until(last_speaker_action_reachable)
    assert window.get_height() == height, (height, window.get_height())
    page.update(replace(page.meeting, metadata_editable=False))
    page.name_speakers("guest")
    assert page.speakers_dialog.closed
    assert "Name the speakers" not in labels(page.speakers)
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        "PASS speaker GTK: validation/name/clear/failure/retry/sample/merge/Undo/Cancel/read-only/20-speaker scrolling"
    )
