# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""A metadata-only header refresh also invalidates the row's visible chips."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cassini_talk.meeting_text import reading_markup
from cassini_talk.portable import Word
from cassini_talk.transcript import Turn


def test_header_refresh_updates_chip_names_and_theme_without_replacing_rows():
    gi = pytest.importorskip("gi")
    for name, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
        gi.require_version(name, version)
    from cassini_talk.meeting_page import MeetingPage

    names, dark = {"a": "Ada", "b": "Ben"}, [False]
    turn = Turn(0, "a", [Word("a", 0, 400, "Before")], 0, 0, 400)
    turn.asides = [(0, Turn(1, "b", [Word("b", 200, 300, "Reply")], 1, 200, 300))]
    words, row = SimpleNamespace(set_markup=Mock(), get_root=lambda: None), object()
    page = SimpleNamespace(
        _header_source=99,
        _header_pending=[],
        bound={0: (turn, words, row)},
        _refresh_header=Mock(),
        player=SimpleNamespace(position=0),
        _style_manager=SimpleNamespace(get_dark=lambda: dark[0]),
        meeting=SimpleNamespace(speaker_for=lambda s: SimpleNamespace(name=names[s])),
    )
    page._render_words = lambda turn, words: MeetingPage._render_words(page, turn, words)
    page._turn_markup = lambda t: reading_markup(t, 0, names.__getitem__, dark=dark[0])

    def refresh():
        page._header_pending.append((turn, words, row))
        MeetingPage._refresh_headers(page)

    refresh()
    assert "Ben:" in words.set_markup.call_args.args[0]
    refresh()
    assert words.set_markup.call_count == 1
    names["b"] = "Bea"
    refresh()
    assert "Bea:" in words.set_markup.call_args.args[0]
    dark[0] = True
    refresh()
    assert 'background="#ffffff"' in words.set_markup.call_args.args[0]
    assert words.set_markup.call_count == 3
    assert page.bound[0][2] is row
