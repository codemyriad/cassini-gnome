#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""L-34/L-35 (D99): starring, the Library All / Starred row and the unified Search filter row."""

import json
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
from gi.repository import Gio, Gtk  # noqa: E402
from painted import apply_screenshot_scheme, painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))

from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.strings import text  # noqa: E402


def until(check):
    dispatch_until(check, "Starring GUI did not complete")


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def labels(widget):
    return [node.get_text() for node in widgets(widget) if isinstance(node, Gtk.Label)]


def menu_labels(model):
    found = []
    for index in range(model.get_n_items()):
        value = model.get_item_attribute_value(index, "label", None)
        if value is not None:
            found.append(value.get_string())
        for link in ("section", "submenu"):
            child = model.get_item_link(index, link)
            if child is not None:
                found.extend(menu_labels(child))
    return found


def visible_meeting_titles(window):
    return [
        row.meeting.title
        for row in widgets(window.rows)
        if isinstance(row, Gtk.ListBoxRow) and hasattr(row, "meeting") and row.get_child_visible()
    ]


def row_for(window, title):
    return next(r for r in widgets(window.rows) if getattr(getattr(r, "meeting", None), "title", None) == title)


def stars_on_disk(root):
    path = root / ".cassini/stars.json"
    return set(json.loads(path.read_text())["starred"]) if path.exists() else set()


def chip_labels(page):
    chips = next(node for node in widgets(page) if isinstance(node, Gtk.FlowBox))
    return [
        child.get_child().get_label()
        for child in widgets(chips)
        if isinstance(child, Gtk.FlowBoxChild) and child.get_child() is not None
    ]


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    library = tmp / "fixtures/main/root"
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(library),
        CASSINI_SCREENSHOT_ACTIVITY=str(tmp / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    apply_screenshot_scheme()
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    app = CassiniApplication("com.gocassini.StarringTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending and window._stars is not None and window._stars.loaded)
    settings = app.recording_settings
    assert settings.get_string("library-filter") == "all"

    # Library at rest: All / Starred linked toggles, All by default, catalog words.
    assert window.library_filter_row.get_mapped()
    assert (window.filter_all.get_label(), window.filter_starred.get_label()) == (
        text("library.filter.all"),
        text("library.filter.starred"),
    )
    assert window.filter_all.get_active() and not window.filter_starred.get_active()
    everything = visible_meeting_titles(window)
    assert "Budget review" in everything and "Onboarding walkthrough" in everything

    # Starred with nothing starred shows the shared empty line.
    window.filter_starred.set_active(True)
    until(lambda: window.starred_empty.get_mapped())
    assert window.starred_empty.get_label() == text("library.starred.empty")
    assert visible_meeting_titles(window) == []
    assert settings.get_string("library-filter") == "starred"
    window.filter_all.set_active(True)
    until(lambda: not window.starred_empty.get_visible())
    assert settings.get_string("library-filter") == "all"
    assert visible_meeting_titles(window) == everything

    # Row context menu: Star; the row gains a glyph and an accessible "Starred".
    budget = row_for(window, "Budget review")
    popover = window._row_menu(budget, 100, 20)
    assert text("meeting.star") in menu_labels(popover.get_menu_model())
    popover.popdown()
    window.activate_action("win.toggle-star", None)
    until(lambda: budget.meeting.key in stars_on_disk(library))
    until(lambda: any(isinstance(n, Gtk.Image) and n.get_icon_name() == "starred-symbolic" for n in widgets(budget)))
    assert row_for(window, "Budget review").meeting.key in window._stars.starred
    popover = window._row_menu(row_for(window, "Budget review"), 100, 20)
    assert text("meeting.unstar") in menu_labels(popover.get_menu_model())
    popover.popdown()

    # Meeting header: the star button and the ⋯ menu say Unstar for a starred meeting.
    window.open_meeting(next(m for m in window.snapshot.meetings if m.title == "Interview 14"))
    until(lambda: window.meeting_page is not None and window.meeting_page.meeting.title == "Interview 14")
    page = window.meeting_page
    assert page.star_button.get_visible() and page.star_button.get_tooltip_text() == text("meeting.star")
    assert text("meeting.star") in menu_labels(page.more_menu)
    page.star_button.emit("clicked")
    until(lambda: page.star_button.get_tooltip_text() == text("meeting.unstar"))
    assert page.star_button.get_icon_name() == "starred-symbolic"
    assert text("meeting.unstar") in menu_labels(page.more_menu)
    interview_key = page.meeting.key
    until(lambda: interview_key in stars_on_disk(library))
    window.back_to_library()

    # Starred at rest shows exactly the starred meetings, with their day headings.
    window.filter_starred.set_active(True)
    until(lambda: sorted(visible_meeting_titles(window)) == ["Budget review", "Interview 14"])
    assert not window.starred_empty.get_visible()
    headings = [r for r in widgets(window.rows) if isinstance(r, Gtk.ListBoxRow) and getattr(r, "_group_keys", None)]
    assert any(h.get_child_visible() for h in headings)
    assert all(h.get_child_visible() == any(k in window._stars.starred for k in h._group_keys) for h in headings), (
        "a day heading shows only when one of its meetings does"
    )

    # Search opened from Starred carries Starred; chips in the L-35 order.
    window.activate_action("win.search", None)
    search = window.search_page
    until(lambda: not search.pending)
    assert not window.library_filter_row.get_mapped(), "the at-rest row hides while Search is open"
    assert chip_labels(search) == [
        text("search.filter.starred"),
        text("search.filter.speakerNone"),
        text("search.filter.anyDate"),
        text("search.filter.recordedByMe"),
    ]
    assert search.starred.get_active()
    assert "Has summary" not in labels(search)
    assert labels(search.date_button.get_popover()) == [
        text("search.filter.anyDate"),
        text("search.filter.date.today"),
        text("search.filter.date.week"),
        text("search.filter.date.month"),
        text("search.filter.date.year"),
    ]
    search.entry.set_text("budget")
    until(lambda: search.pending)
    until(lambda: not search.pending)
    assert search.hits and {hit.key for hit in search.hits} <= window._stars.starred
    until(
        lambda: any(isinstance(n, Gtk.Image) and n.get_icon_name() == "starred-symbolic" for n in widgets(search.list))
    )
    # Turning the Search-only Starred off widens the hits; Library keeps its choice.
    search.starred.set_active(False)
    until(lambda: not search.pending and not {hit.key for hit in search.hits} <= window._stars.starred)
    assert window.library_filter == "starred" and settings.get_string("library-filter") == "starred"

    # Recorded by me keeps only origin = own: the Imported meeting never matches.
    search.entry.set_text("onboarding")
    until(lambda: search.pending)
    until(lambda: not search.pending and search.hits)
    imported = {m.key for m in window.snapshot.meetings if m.origin != "own"}
    assert {hit.key for hit in search.hits} & imported
    search.own.set_active(True)
    until(lambda: search.pending)
    until(lambda: not search.pending)
    assert not {hit.key for hit in search.hits} & imported

    # This year narrows by local 1 January; combined filters AND together.
    search.entry.set_text("budget")
    search.choose_date("year")
    search.choose_speaker("Marta")
    until(lambda: search.pending)
    until(lambda: not search.pending)
    assert search.date_button.get_label() == text("search.filter.date.year")
    year_hits = {hit.key for hit in search.hits}
    assert year_hits
    search.starred.set_active(True)
    until(lambda: search.pending)
    until(lambda: not search.pending)
    assert {hit.key for hit in search.hits} <= year_hits & window._stars.starred

    # Closing Search resets every transient chip; Library is still on Starred.
    window.back_to_library()
    until(lambda: window.search_page is None and window.sidebar_stack.get_visible_child_name() == "library")
    assert window.filter_starred.get_active() and window.library_filter == "starred"
    until(lambda: window.library_filter_row.get_mapped())
    window.filter_all.set_active(True)
    window.activate_action("win.search", None)
    fresh = window.search_page
    until(lambda: not fresh.pending)
    assert not fresh.starred.get_active(), "All at rest opens Search without Starred"
    assert fresh.speaker is None and fresh.date == "any" and not fresh.own.get_active()
    window.back_to_library()
    until(lambda: window.search_page is None)
    window.filter_starred.set_active(True)
    window.activate_action("win.search", None)
    carried = window.search_page
    until(lambda: not carried.pending)
    assert carried.starred.get_active() and carried.speaker is None and carried.date == "any"
    assert not carried.own.get_active()
    window.back_to_library()
    until(lambda: window.search_page is None)

    # Unstarring the last starred meetings brings back the empty line.
    for title in ("Budget review", "Interview 14"):
        meeting = next(m for m in window.snapshot.meetings if m.title == title)
        window.toggle_star(meeting)
    until(lambda: window.starred_empty.get_mapped())
    until(lambda: stars_on_disk(library) == set())
    window._stars.flush()
    window.filter_all.set_active(True)
    until(lambda: visible_meeting_titles(window) == everything)
    if out := os.environ.get("CASSINI_SCREENSHOT_OUT"):
        # Board: the starred Library row and the meeting header star, side by side.
        meeting = next(m for m in window.snapshot.meetings if m.title == "Budget review")
        window.toggle_star(meeting)
        until(lambda: meeting.key in stars_on_disk(library))
        window.rows.select_row(row_for(window, "Budget review"))
        until(lambda: window.meeting_page is not None and window.meeting_page.star_button.get_mapped())
        assert window.filter_all.get_active() and not window.filter_starred.get_active()
        node = painted_node(window)
        texture = window.get_renderer().render_texture(node, node.get_bounds())
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        assert texture.save_to_png(out)

    window.close()
    app.do_shutdown()
    print("PASS: starring, All / Starred at rest, L-35 Search filters, carry-over and reset in private Xvfb")
