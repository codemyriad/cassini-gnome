#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D112 (L-86): the speaker row, Background voice, one Save with an Undo toast, and folded turns."""

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
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_store import read_metadata  # noqa: E402
from cassini_talk.meeting_text import reading_turns, speaker_turns  # noqa: E402
from cassini_talk.portable import Word  # noqa: E402

# A folded run must cost what a few rows cost, not what its thousands of rows would (review #481:
# 5,000 folded turns built 5,003 row widgets in one 2.24 s allocation). Thread CPU, never wall clock.
FOLDED_FRAME_CPU_MS = 500


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


def paint(window):
    loop = GLib.MainLoop()
    clock = window.get_frame_clock()
    handler = clock.connect("after-paint", lambda *_: loop.quit())
    window.add_tick_callback(lambda *_: GLib.SOURCE_REMOVE)
    try:
        loop.run()
    finally:
        clock.disconnect(handler)


class FrameCpu:
    """Thread CPU spent inside each frame (layout and paint)."""

    def __init__(self, window):
        self.clock = window.get_frame_clock()
        self.cpu, self.start = [], 0
        self.handlers = [
            self.clock.connect("before-paint", lambda *_: setattr(self, "start", time.thread_time_ns())),
            self.clock.connect("after-paint", lambda *_: self.cpu.append((time.thread_time_ns() - self.start) / 1e6)),
        ]

    def close(self):
        for handler in self.handlers:
            self.clock.disconnect(handler)
        return max(self.cpu, default=0)


def interrupted_words(host, reply, closing):
    """*host*'s sentence with *reply*'s answer inside it, then *closing* on their own."""
    return [
        Word(host, 0, 1000, "We should"),
        Word(reply, 900, 1200, "Yes."),
        Word(host, 1000, 2000, "ship it."),
        Word(closing, 2200, 2600, "Agreed."),
    ]


def synthetic(model, words, background):
    speakers = tuple(replace(s, background=s.id in background) for s in model.speakers)
    return replace(
        model, speakers=speakers, words=words, turns=reading_turns(words), listening_turns=speaker_turns(words)
    )


def find(page, query):
    page.find_entry.set_text(query)
    until(lambda: page._find_query == query)
    return page._find_matches


def press(page, key):
    assert page.list.navigation.key(None, key, 0, Gdk.ModifierType(0))


def focused_on(page, index):
    until(lambda: page.list.navigation.focused() == index, f"focus never reached row {index}")


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

    # Remember switched alone on another speaker is part of the Save (review #481).
    remembered = []
    store = app.voice_store
    original_remember = store.remember
    store.remember = lambda directory, identifier, *args, **kwargs: remembered.append(identifier)
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voices_loaded and dialog.get_mapped())
    dialog.chips["dario"].set_active(True)
    dialog.remember.set_active(not dialog.remember.get_active())
    dialog.chips["marta"].set_active(True)
    # Without diarization models the fixture has no voice sample; Save only needs one to exist.
    dialog.voice_items.setdefault("dario", {"seconds": 10})
    assert not dialog.voice_state["readOnly"]
    dialog.save.emit("clicked")
    until(lambda: dialog.closed, "Save did not finish")
    store.remember = original_remember
    assert "dario" in remembered, remembered

    # Inline replies are judged by who said them: a background reply drops out of a participant's row
    # and out of Find; a participant's reply inside a background host stays in view and is found.
    model = page.meeting
    window.open_meeting(synthetic(model, interrupted_words("marta", "dario", "guest"), {"dario"}), external=True)
    page = window.meeting_page
    until(lambda: page.list.ready and 0 in page.bound)
    assert not any(run is not None for run in page._runs())
    until(lambda: "ship it." in page.bound[0][1].get_label())
    assert "Yes." not in page.bound[0][1].get_label() and "Dario" not in page.bound[0][1].get_label()
    assert not find(page, "yes") and len(find(page, "ship")) == 1
    page.clear_find()
    window.open_meeting(synthetic(model, interrupted_words("marta", "dario", "guest"), {"marta"}), external=True)
    page = window.meeting_page
    until(lambda: page.list.ready and 0 in page.bound)
    assert not page.row_hidden(0) and not page.bound[0][2]._fold.get_visible()
    until(lambda: "Yes." in page.bound[0][1].get_label())
    assert [m.row_index for m in find(page, "yes")] == [0] and not find(page, "ship")
    page.clear_find()

    # A 5,000-turn folded run: only the rows on screen get widgets, frames stay cheap, and the
    # arrow keys step over the run in one press.
    count = 5000
    words = [Word("marta", 0, 500, "Opening")]
    words += [Word(("dario", "guest")[i % 2], (i + 1) * 1000, (i + 1) * 1000 + 500, f"w{i}") for i in range(count)]
    words.append(Word("marta", (count + 2) * 1000, (count + 2) * 1000 + 500, "Closing"))
    frames = FrameCpu(window)
    window.open_meeting(synthetic(model, words, {"dario", "guest"}), external=True)
    page = window.meeting_page
    until(lambda: page.list.ready and 1 in page.bound)
    paint(window)
    worst = frames.close()
    last = count + 1
    assert len(page._turns) == count + 2 and page._runs()[1] == (1, 2)
    assert all(page.row_hidden(i) for i in (2, count // 2, count))
    assert set(page.list.rows) <= {0, 1, last}, sorted(page.list.rows)[:10]
    assert worst < FOLDED_FRAME_CPU_MS, f"folded run frame CPU {worst:.1f} ms"
    print(f"folded run: {len(page.list.rows)} row widgets, worst frame CPU {worst:.1f} ms")
    page.list.navigation.request(0)
    focused_on(page, 0)
    press(page, Gdk.KEY_Down)
    focused_on(page, 1)
    press(page, Gdk.KEY_Down)
    focused_on(page, last)
    press(page, Gdk.KEY_Up)
    focused_on(page, 1)
    press(page, Gdk.KEY_End)
    focused_on(page, last)
    press(page, Gdk.KEY_Home)
    focused_on(page, 0)
    assert not any(page.row_hidden(i) for i in page.list.rows)

    window._fixture_activity = window._activity = {}
    window.monitor.capture_activity = lambda: {}
    app.begin_exit()
    window.close()
    app.do_shutdown()

print("PASS background voices: speaker row, kept forms, one Save + Undo toast, names row, fold/open, greyed name")
