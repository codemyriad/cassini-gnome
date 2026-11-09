# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading-order matches and zero-only fallback extents from D84 review r2."""

import pytest

from cassini_talk.meeting_text import FollowRows, reader_turns
from cassini_talk.portable import Word
from cassini_talk.transcript import Turn, _spans, find_matches


def test_reader_matches_visit_the_chip_at_its_inline_seam_once():
    owners = reader_turns(
        [
            Word("a", 0, 1000, "hit before"),
            Word("b", 900, 1100, "hit reply"),
            Word("a", 1000, 2000, "hit after"),
            Word("c", 3000, 3400, "hit later"),
        ]
    )
    host, later = owners[:2]
    aside = host.asides[0][1]
    assert find_matches(owners, "hit") == [(host.index, 0), (aside.index, 0), (host.index, 1), (later.index, 0)]
    assert find_matches(owners, "HIT") == find_matches(owners, "hit")
    assert find_matches(owners, " ") == []


@pytest.mark.parametrize(
    "words,point",
    [
        ([Word("b", 1900, 1900, "Yes"), Word("b", 2600, 2600, ",")], 1900),
        ([Word("a", 100, 100, "("), Word("a", 200, 200, "hello")], 200),
    ],
)
def test_zero_only_fallback_uses_the_word_bounded_point(words, point):
    assert _spans(words) == [(point, point)]
    row = Turn(0, words[0].speaker, words, 0, point, point)
    follow = FollowRows([row])
    assert follow.at(point) == 0
    assert follow.at(point + 1) == -1
    assert follow.at((words[0].start_ms + words[-1].end_ms) // 2) == -1
