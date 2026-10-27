# SPDX-License-Identifier: GPL-3.0-or-later
import runpy
from pathlib import Path

assess = runpy.run_path(str(Path(__file__).parent / "whistle/golden_check.py"))["assess_words"]


def test_d11_3_accepts_at_most_one_frame_only_with_identical_inputs():
    expected = [dict(text="word", startMs=100, endMs=200)]
    shifted = [dict(text="word", startMs=180, endMs=280)]
    hashes = ["a" * 64]
    assert assess(shifted, expected, hashes, hashes)["accepted"]
    assert not assess(shifted, expected, ["b" * 64], hashes)["accepted"]
    assert not assess(shifted, expected, [], [])["accepted"]
    assert not assess([dict(text="word", startMs=181, endMs=281)], expected, hashes, hashes)["accepted"]
    assert not assess([dict(text="different", startMs=100, endMs=200)], expected, hashes, hashes)["accepted"]
    assert not assess([], expected, hashes, hashes)["accepted"]
    assert assess(expected, expected, hashes, hashes)["exactWords"]
