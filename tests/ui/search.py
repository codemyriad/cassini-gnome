#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Search menus, phrase matching, context playback, refresh and Meeting navigation."""

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
from gi.repository import Gio, Gst, Gtk  # noqa: E402
from search_bindings import check_playability_bindings  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_store import read_metadata, update_metadata  # noqa: E402
from cassini_talk.player import Player  # noqa: E402
from cassini_talk.strings import text  # noqa: E402


def until(check):
    dispatch_until(check, "Search GUI did not complete")


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def labels(widget):
    return [node.get_text() for node in widgets(widget) if isinstance(node, Gtk.Label)]


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
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
    check_playability_bindings()
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

    # Search badges belong to the hit's meeting, independently of Library selection.
    target = next(m for m in page._meetings.values() if m.title == "Budget review")
    window.selected = next(m for m in window.snapshot.meetings if m.key != target.key)

    def remember_target():
        speakers = read_metadata(target.directory)["speakers"]
        next(s for s in speakers if s["id"] == "marta")["provenance"] = "recognised"
        update_metadata(target.directory, speakers=speakers)
        store = app.voice_store
        store.refresh()
        store.commit_job(target.directory, {"marta": dict(embedding=[1.0] + [0.0] * 511, seconds=8)}, 0)
        store.remember(target.directory, "marta", "Marta")
        return store.snapshot()["voices"][0]["id"]

    future = window.monitor._executor.submit(remember_target)
    until(future.done)
    voice_id = future.result()
    until(
        lambda: (
            not page.pending
            and next(s for s in page._meetings[target.key].speakers if s.id == "marta").provenance == "recognised"
        )
    )
    page.selection.set_selected(next(i for i, h in enumerate(page.hits) if h.key == target.key))
    until(lambda: "auto" in labels(page.context))
    future = window.monitor._executor.submit(app.voice_store.forget, voice_id)
    until(future.done)
    future.result()
    until(lambda: not page.pending and "auto" not in labels(page.context))
    assert next(s for s in page._meetings[target.key].speakers if s.id == "marta").name == "Marta"

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
    opened = window.meeting_page
    assert opened.meeting.title == "Budget review" and opened._exact
    target = opened._follow_rows.at(872000)
    assert target >= 0
    until(lambda: opened.list.viewport_ready and target in opened.list.visible_rows)
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
    # D35 title parity: use a real metadata rename, then remove the transcript
    # reference to test an audio-only meeting through the actual worker/bindings.
    target = next(m for m in window.snapshot.meetings if m.title == "Budget review" and m.origin == "own")
    update_metadata(target.directory, title="Supplier <meeting> & team", titleSource="user")
    window.monitor.refresh_now()
    fresh.entry.set_text("suppl team")
    until(
        lambda: (
            not fresh.pending
            and len(fresh.hits) == 1
            and fresh.hits[0].key == target.key
            and fresh.hits[0].turn == -1
            and bool(fresh.hits[0].title_markup)
        )
    )
    assert fresh.hit.turn == -1 and fresh.hit.position_ms == 0
    assert ">Supplier</span> &lt;meeting&gt; &amp;" in fresh.hit.title_markup
    assert ">team</span>" in fresh.hit.title_markup
    assert "background=" not in fresh.hit.quote_markup
    assert fresh._speaker(fresh.hit) is None
    assert "Supplier <meeting> & team" in labels(fresh.context)
    until(lambda: "Supplier <meeting> & team" in labels(fresh.list))
    assert not any(value.startswith(" · ") for value in labels(fresh.list))
    update_metadata(target.directory, transcript=dict(status="none", reference=None))
    window.monitor.refresh_now()
    until(
        lambda: (
            not fresh.pending
            and fresh.hit is not None
            and fresh.hit.key == target.key
            and not fresh._meetings[target.key].words
            and not fresh.hit.quote_markup
        )
    )
    assert fresh.hit.turn == -1 and fresh._speaker(fresh.hit) is None
    assert not fresh._turns[target.key]
    empty_quote = text("common.quote", quote="")
    until(lambda: empty_quote in labels(fresh.list))
    quotes = [node for node in widgets(fresh.list) if isinstance(node, Gtk.Label) and node.get_text() == empty_quote]
    assert quotes and all(not node.get_visible() for node in quotes), "Audio-only results must not show an empty quote"
    seeks, seek = [], window.player.seek

    def observed_seek(position):
        seeks.append(position)
        return seek(position)

    window.player.seek = observed_seek
    fresh.play.emit("clicked")
    until(lambda: window.player.playing)
    assert seeks[-1] == 0, "A title match must not seek to a fabricated word"
    fresh.open.emit("clicked")
    until(lambda: window.meeting_page is not None and window.search_page is None)
    assert window.meeting_page.meeting.title == "Supplier <meeting> & team"
    assert seeks[-1] == 0
    window.back_to_library()
    window.close()
    app.do_shutdown()
    print("PASS: Search phrase/menus/AND/date/order/reset/seek/refresh/navigation in private Xvfb")
