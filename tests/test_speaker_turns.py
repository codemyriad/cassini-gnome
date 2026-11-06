# SPDX-License-Identifier: GPL-3.0-or-later
"""D70 listening boundaries are independent of transcript layout (E1)."""

import json
from collections import Counter
from pathlib import Path

from cassini_talk.meeting_text import reading_turns, speaker_turns
from cassini_talk.portable import Word
from cassini_talk.speaker_merges import apply_merges


def test_shared_english_synthetic_vector():
    vector = json.loads((Path(__file__).parent / "ux/listen-by-speaker/turns.json").read_text())
    words = tuple(Word(w["speaker"], w["startMs"], w["endMs"], w["text"]) for w in vector["items"])
    turns = speaker_turns(words)
    assert [
        dict(index=t.index, speaker=t.speaker, startMs=t.start_ms, endMs=t.end_ms, wordIndices=list(t.word_indices))
        for t in turns
    ] == vector["expectedTurns"]
    assert Counter(t.speaker for t in turns) == {s["id"]: s["turns"] for s in vector["speakers"]}
    assert len(turns) == 378 and len(vector["speakers"]) == 14
    assert sum(s["turns"] == 1 for s in vector["speakers"]) == 8
    assert turns[0].end_ms == 59100 and len(turns[0].words) == 122
    assert len(reading_turns(turns[0].words)) > 1
    assert min(t.end_ms - t.start_ms for t in turns) == 300
    assert tuple(w for t in turns for w in t.words) == words
    assert vector["language"] == "en"


def test_turns_after_merge_rejoin_runs_without_changing_words():
    words = (Word("a", 0, 100, "We"), Word("b", 100, 200, "agree."), Word("a", 90000, 90100, "Next."))
    assert len(speaker_turns(words)) == 3
    merged = apply_merges(words, [{"from": "b", "into": "a"}])
    turns = speaker_turns(merged)
    assert len(turns) == 1 and turns[0].end_ms == 90100
    assert turns[0].word_indices == (0, 1, 2)
    assert turns[0].text == "We agree. Next."
    assert words[1].speaker == "b"


def test_overlap_aside_is_its_own_turn_and_keeps_original_indices():
    words = (Word("a", 700, 1000, "Continue."), Word("a", 0, 900, "Hello."), Word("b", 300, 600, "Yes."))
    turns = speaker_turns(words)
    assert [(t.speaker, t.start_ms, t.end_ms, t.word_indices) for t in turns] == [
        ("a", 0, 900, (1,)),
        ("b", 300, 600, (2,)),
        ("a", 700, 1000, (0,)),
    ]
    assert all(t.words[0] is words[t.word_indices[0]] for t in turns)


def test_equal_starts_keep_transcript_order_and_empty_is_empty():
    words = (Word("b", 0, 100, "First."), Word("a", 0, 100, "Second."), Word("b", 100, 200, "Third."))
    assert [t.speaker for t in speaker_turns(words)] == ["b", "a", "b"]
    assert speaker_turns(()) == ()


def test_overlapping_same_speaker_words_preserve_longest_end():
    words = (Word("a", 0, 1000, 'He said "hello'), Word("a", 100, 200, 'world".'))
    (turn,) = speaker_turns(words)
    assert turn.end_ms == 1000 and turn.text == "He said “hello world”."
    assert turn.words == words
