# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Metadata and listening ticks retain transcript rows and untouched markup."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cassini_talk.library import LibrarySpeaker
from cassini_talk.meeting_text import changed_header_rows, reading_markup_signature
from cassini_talk.portable import Word
from cassini_talk.transcript import Turn


def turn(index, speaker):
    return Turn(index, speaker, [Word(speaker, 0, 400, "word")], index, 0, 400)


def signature(speakers, *, matches=(), dark=False, merges=None, language="en"):
    return dark, frozenset(matches), tuple(speakers), merges, language


def test_rename_refreshes_only_rows_that_display_that_speaker():
    speakers = [LibrarySpeaker(s, s.upper(), "user", 0, i) for i, s in enumerate("abcd")]
    rows = [turn(i, s) for i, s in enumerate("abcd")]
    rows[1].over = ["a"]
    rows[2].asides = [(0, turn(4, "a"))]
    renamed = [replace(s, name="Ada") if s.id == "a" else s for s in speakers]
    assert changed_header_rows(rows, signature(speakers), signature(renamed)) == tuple(rows[:3])
    # Duration/meter updates do not change any header label or colour.
    timed = [replace(s, talk_ms=12000) for s in speakers]
    assert changed_header_rows(rows, signature(speakers), signature(timed)) == ()
    assert changed_header_rows(rows, signature(speakers), signature(speakers, matches=["b"])) == (rows[1],)


@pytest.mark.parametrize("change", [dict(dark=True), dict(language="it"), dict(merges=[dict(from_="a", into="b")])])
def test_theme_language_and_merge_changes_refresh_all_headers(change):
    speakers = [LibrarySpeaker("a", "Ada", "user", 0, 0)]
    rows = [turn(0, "a"), turn(1, "unknown")]
    assert changed_header_rows(rows, signature(speakers), signature(speakers, **change)) == tuple(rows)
    assert changed_header_rows(rows, None, signature(speakers)) == tuple(rows)


def test_markup_changes_at_word_boundaries_or_chip_label_changes():
    row = Turn(0, "a", [Word("a", 0, 1000, "Before"), Word("a", 1100, 2100, "after")], 0, 0, 2100)
    row.asides = [(0, Turn(1, "b", [Word("b", 900, 1200, "Reply")], 1, 900, 1200))]
    names = dict(a="Ada", b="Ben")

    def key(position):
        return reading_markup_signature(row, position, names.__getitem__)

    assert key(1001) == key(1050)
    assert key(950) != key(1001)  # host word ends
    assert key(1050) != key(1150)  # next host word starts
    assert key(1150) != key(1250)  # chip word ends
    assert key(5000) == key(6000)
    before = key(1001)
    names["a"] = "Ana"
    assert key(1001) == before  # own name is in the header, not word markup
    names["b"] = "Benny"
    assert key(1001) != before
    assert reading_markup_signature(row, 1001, names.__getitem__, dark=True) != key(1001)


def page_class():
    gi = pytest.importorskip("gi")
    for name, version in [("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")]:
        gi.require_version(name, version)
    from cassini_talk.meeting_page import MeetingPage

    return MeetingPage


def test_two_queued_renames_preserve_pending_targets_without_unrelated_rows(monkeypatch):
    page_type = page_class()
    from gi.repository import GLib

    scheduled = []
    monkeypatch.setattr(GLib, "idle_add", lambda callback: scheduled.append(callback) or 77)
    speakers = [LibrarySpeaker(s, s.upper(), "user", 0, i) for i, s in enumerate("abc")]
    rows = [turn(i, s) for i, s in enumerate("abc")]
    page = SimpleNamespace(
        _header_signature=signature(speakers),
        _header_pending=[],
        _header_source=0,
        bound={row.index: (row, object(), object()) for row in rows},
        _refresh_headers=lambda: None,
    )
    renamed_a = [replace(s, name="Ada") if s.id == "a" else s for s in speakers]
    page_type._queue_transcript_headers(page, signature(renamed_a))
    assert [entry[0].speaker for entry in page._header_pending] == ["a"]
    renamed_b = [replace(s, name="Ben") if s.id == "b" else s for s in renamed_a]
    page_type._queue_transcript_headers(page, signature(renamed_b))
    assert {entry[0].speaker for entry in page._header_pending} == {"a", "b"}
    assert len(scheduled) == 1


def test_paused_and_within_word_ticks_do_not_replace_existing_label_markup(monkeypatch):
    page_type = page_class()
    from gi.repository import Adw

    monkeypatch.setattr(Adw.StyleManager, "get_default", lambda: SimpleNamespace(get_dark=lambda: False))
    row = turn(0, "a")
    page = SimpleNamespace(
        player=SimpleNamespace(position=10), meeting=SimpleNamespace(), _turn_markup=Mock(return_value="word")
    )
    words = SimpleNamespace(set_markup=Mock())
    page_type._render_words(page, row, words)
    page_type._render_words(page, row, words)
    page.player.position = 100
    page_type._render_words(page, row, words)
    assert words.set_markup.call_count == 1
    page.player.position = 400
    page_type._render_words(page, row, words)
    assert words.set_markup.call_count == 2
