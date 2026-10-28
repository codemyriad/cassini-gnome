#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Open/drop import, Library selection and the two-format Export dialog."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import portable  # noqa: E402
from cassini_viewer.exporting import export_meeting  # noqa: E402
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.open_meeting import load_open_meeting  # noqa: E402
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
    assert check(), "Import/export GUI condition timed out"


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
    original = load_open_meeting(ROOT / "ux/library-vectors/assets/onboarding.opus")
    incoming = export_meeting(original, tmp / "Downloads")
    before = hashlib.sha256(incoming.read_bytes()).hexdigest()
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
    app = CassiniApplication("com.gocassini.ImportExportTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    initial_received = sum(m.origin == "received" for m in window.snapshot.meetings)
    app.open_in_new_window(Gio.File.new_for_path(str(incoming)))
    until(
        lambda: (
            window.selected
            and window.selected.origin == "received"
            and sum(m.origin == "received" for m in window.snapshot.meetings) == initial_received + 1
        )
    )
    received = window.selected
    assert not window.meeting_page and received.directory.parent == Path(os.environ["CASSINI_LIBRARY_DIR"])
    assert received.audio_file.read_bytes() == incoming.read_bytes()
    assert any("Opened from your Downloads folder." in value for value in labels(window.preview))
    assert not window.received_hint.get_visible()
    until(lambda: not window._pending_selection)
    drop = Gdk.FileList.new_from_array([Gio.File.new_for_path(str(incoming))])
    assert window._drop_files(None, drop, 0, 0)
    until(lambda: not window._opening_files and not window._pending_selection)
    assert sum(m.origin == "received" for m in window.snapshot.meetings) == initial_received + 1
    window.read_button.emit("clicked")
    until(lambda: window.meeting_page is not None and window.player._prerolled)
    window.meeting_page.play.emit("clicked")
    until(lambda: window.player.playing and window.player.position > 0)
    window.monitor.refresh_now()
    assert window.player.playing
    window.meeting_page.play.emit("clicked")
    from unittest.mock import patch

    assert received.audio_size_bytes == received.audio_file.stat().st_size
    with patch.object(Path, "stat", side_effect=AssertionError("Export dialog performed GTK-thread stat")):
        window.show_export()
    until(lambda: window.export_dialog is not None)
    dialog = window.export_dialog
    assert dialog.cassini.get_active() and not dialog.markdown.get_active()
    assert dialog.names.get_text() == "The file carries the names in this meeting. Voiceprints are never exported."
    from cassini_viewer.meeting_store import update_metadata

    update_metadata(
        received.directory,
        title="Updated while Export is open",
        titleSource="user",
        notes=[dict(atMs=2, text="New note while Export is open")],
    )
    window.monitor.refresh_now()
    until(lambda: dialog.meeting.title == "Updated while Export is open")
    assert dialog.meeting.notes == (dict(atMs=2, text="New note while Export is open"),)
    assert "Updated while Export is open" in dialog.get_title()
    assert dialog.heading.get_text() == dialog.get_title()

    dialog.destination = tmp / "Exports"
    dialog._update_folder()
    dialog.export_button.emit("clicked")
    until(lambda: dialog.closed and dialog.output is not None)
    exported = portable.read_meeting(str(dialog.output))
    assert portable.verify_audio(str(dialog.output), exported).state == portable.State.OK
    assert len(portable.load_words(exported, exported.default_transcript_id)) == 2
    assert exported.manifest["meeting"]["title"] == "Updated while Export is open"
    assert exported.manifest["x"]["gocassini.com"]["notes"] == [dict(atMs=2, text="New note while Export is open")]
    # The same GAction serves the header, menu and Ctrl+Shift+E.
    window.lookup_action("export").activate(None)
    until(lambda: window.export_dialog is not dialog)
    dialog = window.export_dialog
    dialog.destination = tmp / "Exports"
    dialog.markdown.set_active(True)
    dialog.export_button.emit("clicked")
    until(lambda: dialog.closed and dialog.output is not None)
    assert dialog.output.suffix == ".md" and "## Transcript" in dialog.output.read_text()
    window.back_to_library()
    budget = next(m for m in window.snapshot.meetings if m.title == "Budget review")
    window.open_meeting(budget)
    window.show_export()
    dialog = window.export_dialog
    assert "<b>Dario</b>" in dialog.names.get_label() and dialog.names.get_label().count("<b>") == 1
    dialog.close()
    until(lambda: dialog.closed)
    many = replace(
        budget,
        speakers=tuple(
            replace(s, name=name, provenance="recognised")
            for s, name in zip(budget.speakers, ("Marta", "Dario & Co", "Ana"), strict=True)
        ),
    )
    window.meeting_page.update(many)
    window.show_export()
    dialog = window.export_dialog
    assert "Marta, Dario & Co and Ana, who were recognised automatically" in dialog.names.get_text()
    assert dialog.names.get_label().count("<b>") == 3 and "&amp;" in dialog.names.get_label()
    dialog.close()
    until(lambda: dialog.closed)
    # Cancel through the actual dialog close path while a real long PCM export
    # is running; no mocked encoder, output or user audio device.
    window.show_export()
    dialog = window.export_dialog
    dialog.destination = tmp / "Cancelled export"
    dialog.export_button.emit("clicked")
    until(lambda: bool(list(dialog.destination.glob(".cassini-*"))))
    dialog.close()
    assert dialog.cancelled.is_set(), "Escape/close did not cancel until its animation finished"
    until(lambda: dialog.closed and not list(dialog.destination.iterdir()))
    assert hashlib.sha256(incoming.read_bytes()).hexdigest() == before
    window.back_to_library()
    window._fixture_activity = window._activity = {}
    window.monitor.capture_activity = lambda: {}
    window.close()
    app.comfort.close()
print(
    "PASS Open/drop import and dedupe select Library; folder card; playback; Cassini/Markdown export; complete, escaped recognised-name disclosure"
)
