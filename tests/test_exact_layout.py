# SPDX-License-Identifier: GPL-3.0-or-later
"""Physical GTK blocks retain the semantic viewer paragraph and timed atoms."""

import re

from transcript_long_turn import long_turn_words

from cassini_talk.meeting_text import LAYOUT_WORD_LIMIT, ReadingRows, reading_markup, reading_turns, reading_word
from cassini_talk.portable import Word
from cassini_talk.transcript import Turn


def test_long_rejoined_turn_has_bounded_layout_with_identical_timed_atoms():
    words = long_turn_words()
    semantic = reading_turns(words)
    assert len(semantic) == 1
    assert len(semantic[0].words) == 72000
    assert len(semantic[0].asides) == 899
    layout = semantic.layout
    assert len(layout) > 500
    original = {}
    for position, row in enumerate(layout):
        assert row.index == position
        assert len(row.words) + sum(len(aside.words) for _, aside in row.asides) <= LAYOUT_WORD_LIMIT
        for owner in [row] + [aside for _, aside in row.asides]:
            for index, word in zip(owner.word_indices, owner.words, strict=True):
                assert index not in original
                original[index] = word
                assert layout.follow.at(word.end_ms - 1) == position
        markup = reading_markup(row, -1, str, chip_links=True)
        for uri in re.findall(r'<a href="([^"]+)">', markup):
            assert any(
                reading_word(row, uri) is word for word in words[max(0, row.first_word - 1) : row.first_word + 200]
            )
    assert all(original[i] is word for i, word in enumerate(words))
    assert not layout[-1].has_more
    assert all(row.has_more and row.continuation == (i > 0) for i, row in enumerate(layout[:-1]))


def test_oversized_chip_keeps_synthetic_hit_target_and_one_delimiter_pair():
    host = Turn(0, "a", [Word("a", 0, 100, "Hello")], 0, 0, 100, word_indices=[0])
    words = [Word("b", i + 100, i + 101, "yes") for i in range(300)]
    reply = Turn(1, "b", words, 1, 100, 400, word_indices=list(range(1, 301)))
    host.asides = [(0, reply)]
    layout = ReadingRows([host]).layout
    markup = "".join(reading_markup(row, -1, str, chip_links=True) for row in layout)
    assert markup.count("(<b>b:</b>") == markup.count(")</a>") == 1
    retained = []
    for row in layout:
        for _, part in row.asides:
            retained.extend(part.words)
            assert reading_word(row, "aside:0:anchor") is words[0]
            for i, word in enumerate(part.words):
                assert reading_word(row, f"aside:0:{i}") is word
    assert retained == words
