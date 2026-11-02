# SPDX-License-Identifier: GPL-3.0-or-later
"""Voice windows on a per-person track never span the silence between its turns.

A track's words are all its own speaker's, so no other speaker's word ends a
run; a pause that could be a window by itself (MIN_SEGMENT_MS) ends it instead.
The shared D25 rule (max_pause_ms=None) and its vectors are unchanged.
"""

import json
import struct
from itertools import pairwise

import pytest

pytest.importorskip("numpy")  # Core/Whistle-light profiles need no PCM worker dependencies.

from test_resource_budget import RATE, extractor, voices, write_wav  # noqa: E402, F401

from cassini_talk.voices_logic import MIN_SEGMENT_MS, select_windows  # noqa: E402

SPEECH = 100  # PCM16 value of the speech bursts; silence is zero.


def remote(root, phrases, seconds=300):
    """One remote's track: speech and one-second words only inside `phrases` ((startMs, endMs), ...)."""
    path = root / "track-r1-0.wav"
    frames = write_wav(path, seconds, [])
    with path.open("r+b") as stream:
        for start, end in phrases:
            stream.seek(44 + start * RATE // 1000 * 2)
            stream.write(struct.pack("<h", SPEECH) * ((end - start) * RATE // 1000))
    items = [
        dict(speaker="track-r1", text="word", startMs=at, endMs=min(at + 1000, end))
        for start, end in phrases
        for at in range(start, end, 1000)
    ]
    (root / "transcript.json").write_text(json.dumps({"format": "cassini.words.v1", "items": items}))
    return [dict(id="r1-0", sourceTrackId="r1", kind="remote", path=str(path), frames=frames, sampleRate=RATE)]


def test_windows_of_a_track_that_speaks_twice_stay_inside_its_speech(tmp_path, extractor):  # noqa: F811
    phrases = [(20000, 30000), (200000, 210000)]
    events = voices(tmp_path, remote(tmp_path, phrases))
    (voice,) = [e for e in events if e["type"] == "voice-print"]
    assert voice["speaker"] == "track-r1"
    windows = [(w["startMs"], w["endMs"]) for w in voice["windows"]]
    assert windows and all(any(s <= a and b <= e for s, e in phrases) for a, b in windows), windows
    assert {a // 100000 for a, _ in windows} == {0, 2}, "both turns give windows"
    # The extractor never sees the three silent minutes between the turns.
    assert extractor == [[[SPEECH]] * len(windows)]


def test_a_natural_pause_inside_a_turn_keeps_one_run(tmp_path, extractor):  # noqa: F811
    # 1.2 s without words, under MIN_SEGMENT_MS: still one run, tiled without a gap.
    events = voices(tmp_path, remote(tmp_path, [(20000, 26000), (27200, 33200)]))
    (voice,) = [e for e in events if e["type"] == "voice-print"]
    windows = [(w["startMs"], w["endMs"]) for w in voice["windows"]]
    assert windows[0][0] == 20200 and windows[-1][1] == 33000, windows
    assert all(a[1] == b[0] for a, b in pairwise(windows)), windows


@pytest.mark.parametrize("pause, runs", [(MIN_SEGMENT_MS, 1), (MIN_SEGMENT_MS + 1, 2)], ids=["at-bound", "over"])
def test_a_pause_longer_than_a_segment_ends_the_run(pause, runs):
    words = [dict(speaker="a", startMs=0, endMs=4000), dict(speaker="a", startMs=4000 + pause, endMs=9000 + pause)]
    windows = select_windows([], words, 16000, 16 * 20000, min_seconds=0, max_seconds=None, max_pause_ms=MIN_SEGMENT_MS)
    # One run tiles windows across the pause; two runs leave it out.
    crossing = [w for w in windows["a"] if w[0] < 4000 * 16 < w[1] or w[0] < (4000 + pause) * 16 < w[1]]
    assert bool(crossing) == (runs == 1), windows


def test_without_a_pause_bound_the_shared_rule_is_unchanged():
    words = [dict(speaker="a", startMs=0, endMs=4000), dict(speaker="a", startMs=60000, endMs=64000)]
    windows = select_windows([], words, 16000, 16 * 70000, min_seconds=0, max_seconds=None)
    assert windows["a"][0][0] == 200 * 16 and windows["a"][-1][1] == 63800 * 16, "D25: one run, first to last word"
