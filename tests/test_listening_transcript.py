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
    words = tuple(Word("ada", i * 1000, i * 1000 + 400, "Hello") for i in range(200)) + (
        Word("ben", 201000, 201500, "Agreed"),
    )
    model = SimpleNamespace(words=words, turns=reading_turns(words), listening_turns=speaker_turns(words))
    page = SimpleNamespace()
    MeetingPage._index_listening_rows(page, model)
    assert page._turn_rows == {0: frozenset({0, 1, 2}), 1: frozenset({3})}
    assert page._row_listening == {0: 0, 1: 0, 2: 0, 3: 1}
    assert page._local_turn_index == {0: 0, 1: 0}


def test_reindexed_long_turn_invalidates_previous_scroll_target():
    words = tuple(Word("ada", i * 1000, i * 1000 + 400, "Hello") for i in range(200))
    page = SimpleNamespace()
    short = SimpleNamespace(turns=reading_turns(words[:70]), listening_turns=speaker_turns(words[:70]))
    MeetingPage._index_listening_rows(page, short)
    page._highlight_target = ("ada", 0)
    long = SimpleNamespace(turns=reading_turns(words), listening_turns=speaker_turns(words))
    MeetingPage._index_listening_rows(page, long)
    assert page._turn_rows == {0: frozenset(range(3))}
    assert page._highlight_target is None


def test_folded_reply_listening_highlights_host_and_header_listens_to_host_speaker():
    words = (
        Word("a", 0, 1000, "Before"),
        Word("b", 900, 1200, "Right."),
        Word("a", 1000, 2000, "after"),
        Word("c", 3000, 4000, "Next."),
    )
    model = SimpleNamespace(turns=reading_turns(words), listening_turns=speaker_turns(words))
    page = SimpleNamespace()
    MeetingPage._index_listening_rows(page, model)
    assert page._turn_rows == {0: frozenset({0}), 1: frozenset({0}), 2: frozenset({0}), 3: frozenset({1})}
    assert page._row_listening == {0: 0, 1: 3}
    assert page._local_turn_index == {0: 0, 1: 0, 2: 1, 3: 0}


def test_external_file_keeps_source_indices_for_competing_and_folded_speakers(tmp_path, monkeypatch):
    from pathlib import Path

    from cassini_talk import portable
    from cassini_talk.open_meeting import load_open_meeting

    # A valid portable file may store words in speaker-turn order. Exercise the
    # external loader's handoff to both projections, with one folded reply and
    # a sustained competing speaker whose paragraph must remain addressable.
    path = tmp_path / "external.opus"
    path.write_bytes((Path(__file__).parent / "conformance/opus/001-minimal-v1.opus").read_bytes())
    words = (
        Word("a", 0, 1000, "Before"),
        Word("b", 900, 1200, "Right."),
        Word("a", 1000, 2000, "after"),
        Word("c", 500, 1500, "Longer"),
        Word("c", 1500, 2500, "contribution"),
        Word("c", 2500, 3500, "here."),
    )
    monkeypatch.setattr(portable, "load_words", lambda *_args: list(words))
    meeting = load_open_meeting(path)
    assert meeting.words == words
    assert [row.speaker for row in meeting.turns] == ["a", "c"]
    page = SimpleNamespace()
    MeetingPage._index_listening_rows(page, meeting)
    assert page._turn_rows == {
        0: frozenset({0}),
        1: frozenset({1}),
        2: frozenset({0}),
        3: frozenset({0}),
        4: frozenset({1}),
    }
    assert page._row_listening == {0: 0, 1: 1}
    owners = [owner for row in meeting.turns for owner in [row] + [aside for _, aside in row.asides]]
    assert all(owner.first_word == owner.word_indices[0] for owner in owners)
    assert all(
        word is meeting.words[i] for owner in owners for i, word in zip(owner.word_indices, owner.words, strict=True)
    )
