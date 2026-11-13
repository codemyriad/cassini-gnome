# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synchronous unit checks of real GTK transcript bindings in Boards' display.

Only shell services are stubbed: rows, labels, buttons and production binding
methods are real. No windows, event loops, sleeps, files or audio are needed.
"""

from dataclasses import replace
from datetime import UTC
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from gi.repository import Adw, Gtk

from cassini_talk.i18n import using_language
from cassini_talk.library import LibrarySpeaker, Meeting, PreviewTurn
from cassini_talk.library_window import LibraryWindow
from cassini_talk.meeting_page import MeetingPage
from cassini_talk.portable import Word
from cassini_talk.strings import text
from cassini_talk.tokens import speaker_colour
from cassini_talk.transcript import Turn


def children(widget):
    child = widget.get_first_child()
    while child:
        yield child
        child = child.get_next_sibling()


def labels(widget):
    if isinstance(widget, Gtk.Label):
        yield widget
    for child in children(widget):
        yield from labels(child)


def set_crosstalk(turn, value):
    turn.words = [replace(word, low_confidence=value) for word in turn.words]


def fixture():
    speakers = tuple(
        LibrarySpeaker(identifier, name, "recognised" if identifier == "a" else "user", 0, i)
        for i, (identifier, name) in enumerate((("a", "Ada"), ("b", "Ben"), ("c", "Cara")))
    )
    words = (Word("a", 0, 1000, "Hello"),)
    row = Turn(0, "a", list(words), 0, 0, 1000)
    meeting = Meeting(
        id="gtk-unit",
        directory=Path("/fixture/gtk-unit"),
        title="Transcript bindings",
        started_at_ms=None,
        duration_ms=1000,
        speakers=speakers,
        words=words,
        transcript="done",
        metadata_editable=False,
        preview_turns=(PreviewTurn("a", 0, "Hello", row),),
    )
    return meeting, row


def meeting_bindings():
    meeting, turn = fixture()
    app = SimpleNamespace(voice_store=SimpleNamespace(has_match=lambda *_: True))
    page = SimpleNamespace(
        meeting=meeting,
        window=SimpleNamespace(get_application=lambda: app),
        player=SimpleNamespace(position=0),
        _can_listen=True,
        _exact=False,
        _highlighted_rows=frozenset(),
        _header_signature=None,
        bound={},
        _colour=lambda _speaker: speaker_colour(0),
    )
    for name in ("_build_row", "_turn_markup", "_render_words", "_refresh_header", "_render_header"):
        setattr(page, name, MethodType(getattr(MeetingPage, name), page))
    container = Gtk.Box()
    item = SimpleNamespace(get_item=lambda: SimpleNamespace(turn=turn), set_child=container.append)
    MeetingPage._bind(page, None, item)
    _, words, row = page.bound[turn.index]
    name = row.get_first_child().get_first_child()
    assert isinstance(name, Gtk.Button) and name.get_visible()
    assert row._auto_tag is not None and "Hello" in words.get_label()
    assert not row._over_label.get_visible()

    turn.continuation = True
    page._render_header(turn, row)
    assert not name.get_visible() and row._auto_tag is None
    turn.continuation = False
    turn.over = ["b", "c"]
    page._render_header(turn, row)
    assert name.get_visible() and row._auto_tag is not None
    assert row._over_label.get_visible() and row._over_label.get_label() == "(over Ben and Cara)"
    assert row._over_label.get_tooltip_text() == text("meeting.turn.over.tooltip")
    assert Gtk.test_accessible_has_property(row._over_label, Gtk.AccessibleProperty.DESCRIPTION)
    page.meeting = replace(
        meeting, speakers=tuple(replace(s, name="Benny") if s.id == "b" else s for s in meeting.speakers)
    )
    page._render_header(turn, row)
    assert row._over_label.get_label() == "(over Benny and Cara)"
    set_crosstalk(turn, True)
    page._render_header(turn, row)
    assert row._over_label.get_label() == "(over Benny and Cara) · Probably crosstalk"
    turn.over = []
    page._render_header(turn, row)
    assert row._over_label.get_label() == "Probably crosstalk" and row._over_label.get_visible()
    assert row._over_label.get_tooltip_text() is None
    assert not Gtk.test_accessible_has_property(row._over_label, Gtk.AccessibleProperty.DESCRIPTION)
    set_crosstalk(turn, False)
    page._render_header(turn, row)
    assert not row._over_label.get_visible() and row._over_label.get_label() == ""
    print("PASS synchronous Meeting GTK bindings: continuation, Auto, captions, rename and hints", flush=True)


def library_bindings():
    meeting, _turn = fixture()
    app = SimpleNamespace(writing_available=False, voice_store=SimpleNamespace(has_match=lambda *_: True))
    shown, closed = [], []
    window = SimpleNamespace(
        now_ms=1793107080000,
        tz=UTC,
        get_application=lambda: app,
        meeting_page=None,
        search_page=None,
        recording_page=None,
        _external_meeting=False,
        _updating_rows_selection=False,
        split=SimpleNamespace(get_collapsed=lambda: False, set_show_content=Mock()),
        _preview_key=None,
        _loaded_key=None,
        _stop_playback=Mock(),
        player=SimpleNamespace(stop=Mock(), playing=False),
        listen=SimpleNamespace(stop=Mock()),
        _refresh_export=Mock(),
        _recording_line=lambda *_: None,
        _show_right_meeting=lambda value: shown.append(value.key),
        _close_right_meeting=lambda: closed.append(True),
    )

    LibraryWindow._selected(window, None, SimpleNamespace(meeting=meeting))
    assert window.selected is meeting and shown == [meeting.key] and not closed
    LibraryWindow._selected(window, None, None)
    assert window.selected is None and closed == [True]
    live = replace(meeting, lifecycle="recording")
    LibraryWindow._selected(window, None, SimpleNamespace(meeting=live))
    assert window.selected is live and len(closed) == 2 and shown == [meeting.key]
    print("PASS synchronous Library GTK bindings: saved detail mount, empty deselection, and live guard", flush=True)


def check_transcript_bindings():
    Adw.init()
    with using_language("en"):
        meeting_bindings()
        library_bindings()
