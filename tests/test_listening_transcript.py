# SPDX-License-Identifier: GPL-3.0-or-later
"""Long listening turns span reading paragraphs, with exact affected-row sets."""

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from cassini_talk.meeting_page import MeetingPage  # noqa: E402
from cassini_talk.meeting_text import reading_turns, speaker_turns  # noqa: E402
from cassini_talk.portable import Word  # noqa: E402


def test_one_listening_turn_highlights_every_split_reading_paragraph():
    words = tuple(Word("ada", i * 1000, i * 1000 + 400, "Hello") for i in range(70)) + (
        Word("ben", 71000, 71500, "Agreed"),
    )
    model = SimpleNamespace(words=words, turns=reading_turns(words), listening_turns=speaker_turns(words))
    page = SimpleNamespace()
    MeetingPage._index_listening_rows(page, model)
    assert page._turn_rows == {0: frozenset({0, 1, 2}), 1: frozenset({3})}
    assert page._row_listening == {0: 0, 1: 0, 2: 0, 3: 1}
    assert page._local_turn_index == {0: 0, 1: 0}


def test_reindexed_long_turn_invalidates_previous_scroll_target():
    words = tuple(Word("ada", i * 1000, i * 1000 + 400, "Hello") for i in range(140))
    page = SimpleNamespace()
    short = SimpleNamespace(turns=reading_turns(words[:70]), listening_turns=speaker_turns(words[:70]))
    MeetingPage._index_listening_rows(page, short)
    page._highlight_target = ("ada", 0)
    long = SimpleNamespace(turns=reading_turns(words), listening_turns=speaker_turns(words))
    MeetingPage._index_listening_rows(page, long)
    assert page._turn_rows == {0: frozenset(range(5))}
    assert page._highlight_target is None
