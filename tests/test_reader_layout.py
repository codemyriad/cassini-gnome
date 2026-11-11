# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded physical reader rows retain every original search/seek identity."""

from transcript_long_turn import long_turn_words

from cassini_talk.meeting_text import LAYOUT_WORD_LIMIT, reader_turns
from cassini_talk.transcript import visible_turns


def test_long_reader_layout_preserves_original_word_and_chip_keys():
    words = long_turn_words()
    rows = reader_turns(words)
    assert len(visible_turns(rows)) == 1
    assert len(rows.layout) > 500
    seen = set()
    for row in rows.layout:
        owners = [row] + [aside for _, aside in row.asides]
        assert sum(len(owner.words) for owner in owners) <= LAYOUT_WORD_LIMIT
        for owner in owners:
            for word in owner.words:
                key = rows.word_keys[id(word)]
                assert rows[key[0]].words[key[1]] is word
                assert rows.word_rows[key] == row.index
                assert key[0] in rows.row_owners[row.index]
                assert key not in seen
                seen.add(key)
    assert len(seen) == len(words)
    assert {id(w) for owner in rows for w in owner.words} == set(rows.word_keys)


def test_oversized_reader_chip_can_occupy_rows_without_host_words():
    from cassini_talk.portable import Word

    words = [Word("a", 0, 1000, "Opening.")]
    words.extend(Word("b", 900, 1100, "Reply") for _ in range(400))
    words.append(Word("a", 1000, 2000, "Closing."))
    rows = reader_turns(words)
    assert any(not row.words and row.asides for row in rows.layout)
    for key, index in rows.word_rows.items():
        word = rows[key[0]].words[key[1]]
        assert any(
            word is atom
            for owner in [rows.layout[index]] + [aside for _, aside in rows.layout[index].asides]
            for atom in owner.words
        )
    assert all(
        len(row.words) + sum(len(aside.words) for _, aside in row.asides) <= LAYOUT_WORD_LIMIT for row in rows.layout
    )
