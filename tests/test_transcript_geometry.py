# SPDX-License-Identifier: GPL-3.0-or-later
"""Visible height corrections preserve exact prefix lookup and append semantics."""

from bisect import bisect_right
from itertools import accumulate
from random import Random

import pytest

from cassini_talk.transcript_geometry import DisplayPrefix


def test_display_prefix_matches_full_remeasurement_after_corrections_and_growth():
    random = Random(316)
    heights = [random.randrange(20, 200) for _ in range(2000)]
    prefix = DisplayPrefix([0, *accumulate(heights[:400])], len(heights))
    for row in random.sample(range(400), 100):
        heights[row] += random.randrange(-10, 100)
        prefix.set_height(row, heights[row])
    for height in heights[400:]:
        prefix.append(prefix[-1] + height)
    expected = [0, *accumulate(heights)]
    assert prefix[:] == expected
    for point in expected[::17]:
        assert bisect_right(prefix, point + 7) == bisect_right(expected, point + 7)
    assert prefix[-1] == expected[-1]
    assert prefix[::-7] == expected[::-7]
    with pytest.raises(IndexError):
        _ = prefix[len(prefix)]
    with pytest.raises(IndexError):
        _ = prefix[-len(prefix) - 1]
