#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D112 (L-86): the speaker row, Background voice, one Save with an Undo toast, and folded turns."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import Adw, Gio, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_store import read_metadata  # noqa: E402


def until(check, message="Background voices GUI did not complete"):
    dispatch_until(check, message)


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


def stored(directory):
    return {s["id"]: s for s in read_metadata(directory)["speakers"]}


def fold_row(page):
    runs = page._runs()
    index = next(i for i, run in enumerate(runs) if run is not None and run[0] == i)
    page.list.scroll_to(index, Gtk.ListScrollFlags.NONE, None)
    until(lambda: index in page.bound and page.bound[index][2].get_mapped(), "folded row never shown")
    return index, page.bound[index][2]


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
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
    app = CassiniApplication("com.gocassini.BackgroundVoicesTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    meeting = next(m for m in window.snapshot.meetings if m.title == "Budget review")
    directory = meeting.directory
    before = read_metadata(directory)["speakers"]
    window.player.shutdown()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player = FolderPlayer(audio_sink=sink)
    window._connect_player()
    window.open_meeting(meeting)
    page = window.meeting_page
    until(lambda: page.list.ready)
    assert [s.id for s in meeting.speakers] == ["marta", "dario", "guest"], [s.id for s in meeting.speakers]
    assert labels(page.speakers).count("Dario") == 1

    # The speaker row: every speaker, in the order of the names row; the opened one is pressed.
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voices_loaded and dialog.get_mapped())
    assert dialog.save.get_label() == "Save"
    assert list(dialog.chips) == ["marta", "dario", "guest"] and dialog.speaker_row.get_visible()
    assert dialog.chips["guest"].get_active() and not dialog.chips["dario"].get_active()
    assert "speaker-unnamed" in dialog.chips["guest"]._name.get_css_classes()
    dialog.entry.set_text("Giulia")
    assert dialog.chips["guest"]._name.get_label() == "Giulia"

    # Switch without closing: Dario's own form.
    dialog.chips["dario"].set_active(True)
    assert dialog.identifier == "dario" and dialog.heading.get_label() == "Dario"
    assert dialog.entry.get_text() == "Dario" and not dialog.closed
    assert any("Background voice" in value for value in labels(dialog.get_child()))
    assert dialog.remember_card.get_visible() and not dialog.background.get_active()
    dialog.background.set_active(True)
    assert not dialog.entry.get_sensitive() and dialog.entry.get_text() == ""
    assert dialog.entry.get_placeholder_text() == "Not needed for a background voice"
    assert not dialog.remember_card.get_visible() and not dialog.merge.get_sensitive()
    assert not dialog.voices_section.get_sensitive()
    assert "speaker-dot-hollow" in dialog.chips["dario"]._dot.get_css_classes()
    assert "speaker-chip-background" in dialog.chips["dario"]._name.get_css_classes()
    assert dialog.save.get_sensitive()

    # Changes are kept while moving around.
    dialog.chips["guest"].set_active(True)
    assert dialog.identifier == "guest" and dialog.entry.get_text() == "Giulia"
    assert dialog.entry.get_sensitive() and not dialog.background.get_active()
    dialog.chips["dario"].set_active(True)
    assert dialog.background.get_active() and dialog.entry.get_text() == ""

    # One Save applies them all; one toast sums them up.
    dialog.save.emit("clicked")
    until(lambda: dialog.closed and page.meeting.background_speakers == {"dario"}, "Save did not apply")
    speakers = stored(directory)
    assert speakers["guest"]["name"] == "Giulia" and speakers["guest"]["provenance"] == "user"
    assert speakers["dario"]["background"] is True and speakers["dario"]["name"] == "Dario"
    toast = page._speakers_toast
    assert toast is not None and toast.get_title() == "Named Giulia · 1 background voice"
    assert toast.get_button_label() == "Undo"

    # The names row and the Library card leave the background voice out.
    until(lambda: "Dario" not in labels(page.speakers) and "Giulia" in labels(page.speakers))
    row_meeting = next(m for m in window.snapshot.meetings if m.directory == directory)
    assert "Dario" not in window._row_display_fields(row_meeting)[1]

    # Its turns fold into one row, then open in place, greyed.
    index, row = fold_row(page)
    assert row._fold.get_visible() and not row.body.get_visible()
    assert row._fold._label.get_label() == "1 background voice hidden"
    assert row._fold._icon.get_icon_name() == "pan-down-symbolic"
    hidden = [i for i, run in enumerate(page._runs()) if run is not None and run[0] != i]
    assert all(page.row_hidden(i) for i in hidden)
    row._fold.emit("clicked")
    until(lambda: index in page.bound and page.bound[index][2].body.get_visible(), "fold did not open")
    row = page.bound[index][2]
    assert row._fold._icon.get_icon_name() == "pan-up-symbolic"
    assert "background-turn" in row.body.get_css_classes()
    assert not any(page.row_hidden(i) for i in hidden)
    # Find leaves background voices out.
    assert all(not page._row_background(m.row_index) for m in page._find_matches)

    # A greyed name opens Name the speakers on that voice, to switch it back.
    name = row.get_first_child().get_first_child()
    assert name.get_sensitive()
    name.emit("clicked")
    until(lambda: page.speakers_dialog is not dialog and page.speakers_dialog.get_mapped())
    reopened = page.speakers_dialog
    assert reopened.identifier == "dario" and reopened.background.get_active()
    reopened.close()
    until(lambda: reopened.closed)

    # Undo puts everything back.
    toast.emit("button-clicked")
    until(lambda: stored(directory) == {s["id"]: s for s in before} and not page.meeting.background_speakers)
    until(lambda: "Dario" in labels(page.speakers))
    assert not any(run is not None for run in page._runs())

    window._fixture_activity = window._activity = {}
    window.monitor.capture_activity = lambda: {}
    app.begin_exit()
    window.close()
    app.do_shutdown()

print("PASS background voices: speaker row, kept forms, one Save + Undo toast, names row, fold/open, greyed name")
