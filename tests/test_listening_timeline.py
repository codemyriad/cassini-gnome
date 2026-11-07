# SPDX-License-Identifier: GPL-3.0-or-later
"""Readout intervals include silence, overlap and exact ends; snap in pixels."""

import pytest
from test_listen import turn

from cassini_talk.listening_timeline import snap_start, time_index


def test_overlap_returns_to_the_still_audible_voice_then_silence():
    turns = (turn(0, "ada", 1000, 5000), turn(1, "ben", 2000, 3000), turn(2, "cleo", 6000, 7000))
    assert time_index(turns) == ((1000, 2000, 3000, 5000, 6000, 7000), ("ada", "ben", "ada", None, "cleo", None))


@pytest.mark.parametrize("width", [100, 360, 1270])
def test_six_pixel_snap_is_independent_of_duration_and_width(width):
    turns = (turn(0, "ada", 1000, 1100), turn(1, "ada", 9000, 9500))
    mark = 10 + width * 0.1
    assert snap_start(turns, mark + 6, 10, width, 10000) == 1000
    assert snap_start(turns, mark - 6, 10, width, 10000) == 1000
    assert snap_start(turns, mark + 6.01, 10, width, 10000) is None
    assert snap_start((), mark, 10, width, 10000) is None


def test_dense_ticks_keep_the_exact_list_and_nearest_turn():
    turns = tuple(turn(i, "ada", i * 10, i * 10 + 5) for i in range(127))
    assert snap_start(turns, 12.6, 0, 100, 10000) == 1260
    assert snap_start(turns, 12.5, 0, 100, 10000) == 1250
    assert snap_start(turns, 100, 0, 0, 10000) is None
