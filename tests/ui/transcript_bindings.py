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
from cassini_talk.transcript import Turn
from cassini_talk.window import TurnRow


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
        _highlighted_rows=frozenset(),
        _header_signature=None,
        bound={},
        _colour=lambda _speaker: "#1c71d8",
    )
    for name in ("_turn_markup", "_render_words", "_refresh_header", "_render_header"):
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
    meeting, turn = fixture()
    app = SimpleNamespace(writing_available=False, voice_store=SimpleNamespace(has_match=lambda *_: True))
    window = SimpleNamespace(
        now_ms=1793107080000,
        tz=UTC,
        get_application=lambda: app,
        meeting_page=None,
        search_page=None,
        _preview_key=None,
        _preview_signature_value=None,
        _stop_playback=Mock(),
        player=SimpleNamespace(stop=Mock(), playing=False),
        listen=SimpleNamespace(stop=Mock()),
        _refresh_export=Mock(),
        _signature=lambda _snapshot: (),
        snapshot=object(),
        preview=Gtk.Box(orientation=Gtk.Orientation.VERTICAL),
        _recording_line=lambda *_: None,
        publication_line=lambda _meeting: None,
        trash_error_line=lambda _meeting: None,
        _playback_failed=False,
    )
    for name in ("_preview_signature", "_speaker"):
        setattr(window, name, MethodType(getattr(LibraryWindow, name), window))

    def select(value):
        LibraryWindow._selected(window, None, SimpleNamespace(meeting=value))
        return [
            child
            for child in children(window.preview)
            if child.get_last_child() is not None and child.get_last_child().has_css_class("excerpt")
        ]

    def name_labels(line):
        return [label.get_text() for label in labels(line.get_first_child())]

    lines = select(meeting)
    assert "Ada" in name_labels(lines[0])
    unchanged = window.preview.get_first_child()
    select(meeting)
    assert window.preview.get_first_child() is unchanged  # Preserve cached widgets.
    continued = replace(turn, continuation=True)
    next_meeting = replace(meeting, preview_turns=(replace(meeting.preview_turns[0], row=continued),))
    assert next_meeting.preview_turns == meeting.preview_turns
    lines = select(next_meeting)
    assert window.preview.get_first_child() is not unchanged and "Ada" not in name_labels(lines[0])

    uncertain = replace(turn, words=[replace(word, low_confidence=True) for word in turn.words])
    next_meeting = replace(meeting, preview_turns=(replace(meeting.preview_turns[0], row=uncertain),))
    lines = select(next_meeting)
    assert "Probably crosstalk" in name_labels(lines[0])
    overlapping = replace(turn, over=["b", "c"])
    next_meeting = replace(meeting, preview_turns=(replace(meeting.preview_turns[0], row=overlapping),))
    lines = select(next_meeting)
    assert "(over Ben and Cara)" in name_labels(lines[0])
    renamed = replace(
        next_meeting, speakers=tuple(replace(s, name="Benny") if s.id == "b" else s for s in meeting.speakers)
    )
    assert "(over Benny and Cara)" in name_labels(select(renamed)[0])

    # Only timing attestation changes in the stored words; the repaired row
    # changes continuation while the old PreviewTurn key still compares equal.
    attested = replace(meeting.words[0], ends_bounded_by_audio=True)
    repaired = replace(meeting, words=(attested,), preview_turns=(replace(meeting.preview_turns[0], row=continued),))
    select(meeting)
    assert "Ada" not in name_labels(select(repaired)[0])

    aside = Turn(1, "unlisted", [Word("unlisted", 200, 300, "Yes")], 1, 200, 300)
    merged_row = replace(turn, over=["unlisted"], asides=[(0, aside)])
    merged = replace(
        meeting,
        preview_turns=(replace(meeting.preview_turns[0], row=merged_row),),
        metadata={"speakerMerges": [{"from": "unlisted", "into": "b"}]},
    )
    lines = select(merged)
    assert "(over Ben)" in name_labels(lines[0]) and "Ben:" in lines[0].get_last_child().get_text()
    remapped = replace(merged, metadata={"speakerMerges": [{"from": "unlisted", "into": "c"}]})
    assert merged.preview_speakers == remapped.preview_speakers and merged.preview_turns == remapped.preview_turns
    lines = select(remapped)
    assert "(over Cara)" in name_labels(lines[0]) and "Cara:" in lines[0].get_last_child().get_text()

    # A merge can change only a chip's prose, with no caption or visible
    # roster change. That must invalidate the cache independently too.
    chip_only = replace(merged, preview_turns=(replace(merged.preview_turns[0], row=replace(merged_row, over=[])),))
    lines = select(chip_only)
    assert "Ben:" in lines[0].get_last_child().get_text()
    chip_remapped = replace(chip_only, metadata=remapped.metadata)
    lines = select(chip_remapped)
    assert "Cara:" in lines[0].get_last_child().get_text()

    previews = meeting.preview_turns * 4
    assert len(select(replace(meeting, preview_turns=previews))) == 3
    print(
        "PASS synchronous Library GTK bindings: cache, continuation, provenance, captions, names and prose", flush=True
    )


def standalone_bindings():
    _meeting, turn = fixture()
    context = SimpleNamespace(
        palette=SimpleNamespace(css_class=lambda _: "speaker-0"),
        speaker_label={"a": "Ada", "b": "Ben"}.__getitem__,
        turn_markup=lambda _turn: ("Hello", []),
        active_turns=set(),
    )
    row = TurnRow(context)
    row.bind(turn)
    assert row.name.get_visible() and row.dot.get_visible() and not row.over.get_visible()
    assert row.over.get_tooltip_text() is None
    turn.continuation = True
    set_crosstalk(turn, True)
    row.bind(turn)
    assert not row.name.get_visible() and not row.dot.get_visible()
    assert row.over.get_visible() and row.over.get_label() == "Probably crosstalk"
    assert row.over.get_tooltip_text() is None
    assert not Gtk.test_accessible_has_property(row.over, Gtk.AccessibleProperty.DESCRIPTION)
    turn.over = ["b"]
    row.bind(turn)
    assert row.over.get_label() == "(over Ben) · Probably crosstalk"
    assert row.over.get_tooltip_text() == "Simultaneous speech"
    assert Gtk.test_accessible_has_property(row.over, Gtk.AccessibleProperty.DESCRIPTION)
    turn.over, turn.continuation = [], False
    set_crosstalk(turn, False)
    row.bind(turn)
    assert not row.over.get_visible() and row.over.get_tooltip_text() is None
    assert row.name.get_visible() and row.dot.get_visible()
    print("PASS synchronous standalone GTK bindings: captions, continuation and hints", flush=True)


def check_transcript_bindings():
    Adw.init()
    with using_language("en"):
        meeting_bindings()
        library_bindings()
        standalone_bindings()
