# SPDX-License-Identifier: GPL-3.0-or-later
"""D63 #2: a turn never spans a gap range of its own track, in all three groupers.

Cases come from the vendored protocol vectors (conformance/multistream
writedown.json `turns`); corpus turnCases run in test_corpus_pipelines.
"""

import json
from pathlib import Path

import pytest

from cassini_talk.export_model import markdown
from cassini_talk.library import turn_gaps
from cassini_talk.meeting_text import reading_turns
from cassini_talk.portable import Word
from cassini_talk.source_ownership import speaker_gap_ranges
from cassini_talk.transcript import SEAM_GAP_MS, build_turns

ROOT = Path(__file__).parents[1] / "browser-protocol/conformance/multistream"
WRITEDOWN = json.loads((ROOT / "writedown.json").read_text())


def track(identifier="t", *, source="s", start=0, ms=120000, gaps=None, kind="remote"):
    row = dict(id=identifier, kind=kind, sourceTrackId=source, startOffsetMs=start, sampleRate=16000, frames=ms * 16)
    return row if gaps is None else dict(row, gaps=gaps)


def words_of(speaker, pairs):
    return [Word(speaker, start, end, f"w{index}") for index, (start, end) in enumerate(pairs)]


def sizes(turns, speaker):
    return [len(turn.words) for turn in turns if turn.speaker == speaker]


def markdown_sizes(speaker, words, gaps):
    body = markdown(
        dict(
            title="Turns",
            speakers=[dict(id=speaker, name="Guest")],
            items=[dict(speaker=w.speaker, startMs=w.start_ms, endMs=w.end_ms, text=w.text) for w in words],
            notes=[],
            verbatim=True,
            turnGaps=gaps,
        )
    )
    lines = body.split("\n")
    return [len(lines[i + 1].split()) for i, line in enumerate(lines) if line.startswith("**Guest**")]


def grouped(speaker, words, gaps):
    """Turn sizes from build_turns, reading_turns and the Markdown export."""
    return (
        sizes(build_turns(words, gaps=gaps), speaker),
        sizes(reading_turns(words, gaps=gaps), speaker),
        markdown_sizes(speaker, words, gaps),
    )


def rejoined(speaker, words, gaps):
    """build_turns with an empty interjection between each pair, so every join is a 400 ms seam rejoin."""
    woven = [words[0]]
    for word in words[1:]:
        woven += [Word("other", word.start_ms, word.start_ms, ""), word]
    return sizes(build_turns(woven, gaps=gaps), speaker)


@pytest.mark.parametrize("case", WRITEDOWN["turns"], ids=lambda case: case["name"])
def test_writedown_turn_cases_through_every_grouper(case):
    gaps = speaker_gap_ranges([track(gaps=case["gaps"])])
    words = words_of("track-s", case["words"])
    assert grouped("track-s", words, gaps) == (case["turns"],) * 3
    if all(abs(b.start_ms - a.end_ms) <= SEAM_GAP_MS for a, b in zip(words, words[1:], strict=False)):
        assert rejoined("track-s", words, gaps) == case["turns"]


def test_the_seam_rejoin_stops_at_a_gap_range_and_still_joins_without_one():
    gaps = speaker_gap_ranges([track(gaps=[dict(atMs=10000, durationMs=300, frames=4800, kind="jump")])])
    words = words_of("track-s", [(9700, 9950), (10300, 10600)])
    assert rejoined("track-s", words, gaps) == [1, 1]
    assert rejoined("track-s", words, {}) == [2]


def test_files_without_gaps_get_no_breaks():
    words = words_of("track-s", [(9700, 9950), (10300, 10600)])
    assert speaker_gap_ranges([track()]) == {}
    assert grouped("track-s", words, speaker_gap_ranges([track()])) == ([2],) * 3
    assert grouped("track-s", words, None) == ([2],) * 3


def test_only_the_speakers_own_tracks_split_its_turns():
    gap = [dict(atMs=10000, durationMs=300, frames=4800, kind="jump")]
    gaps = speaker_gap_ranges(
        [track("mic", kind="microphone", source=None, gaps=gap), track("b", source="b"), track("sys", kind="system")]
    )
    assert set(gaps) == {"me"}
    pairs = [(9700, 9950), (10300, 10600)]
    for speaker in ("track-b", "others"):
        assert grouped(speaker, words_of(speaker, pairs), gaps) == ([2],) * 3
    assert grouped("me", words_of("me", pairs), gaps) == ([1, 1],) * 3


def test_a_rejoin_and_a_prefix_of_the_rejoining_span():
    # Two spans of one source: the time between them is no gap range; a prefix gap of the second is.
    first = track("d1", start=0, ms=20000, gaps=[])
    second = track("d2", start=24000, ms=26000, gaps=[dict(atMs=24000, durationMs=500, frames=8000, kind="prefix")])
    words = words_of("track-s", [(15000, 15500), (25000, 25400)])
    assert grouped("track-s", words, speaker_gap_ranges([first, dict(second, gaps=[])])) == ([2],) * 3
    assert grouped("track-s", words, speaker_gap_ranges([first, second])) == ([1, 1],) * 3


def test_an_overlap_lane_and_voices_use_their_own_tracks():
    gap = [dict(atMs=10000, durationMs=300, frames=4800, kind="jump")]
    # Two overlapping spans of one source: R64 puts the later one on its own lane.
    gaps = speaker_gap_ranges([track("x1", ms=30000, gaps=[]), track("x2", start=5000, ms=20000, gaps=gap)])
    assert set(gaps) == {"track-s:overlap:1"}
    pairs = [(9700, 9950), (10300, 10600)]
    assert grouped("track-s", words_of("track-s", pairs), gaps) == ([2],) * 3
    for speaker in ("track-s:overlap:1", "track-s:overlap:1~2"):
        assert grouped(speaker, words_of(speaker, pairs), gaps) == ([1, 1],) * 3


def test_silent_tracks_add_no_ranges_and_merges_carry_both_speakers():
    gap = [dict(atMs=10000, durationMs=300, frames=4800, kind="jump")]
    rows = [track("mic", kind="microphone", source=None, gaps=[]), track("b", source="b", gaps=gap)]
    assert speaker_gap_ranges(rows, silent={"b"}) == {}
    merged = speaker_gap_ranges(rows, merges={"track-b": "me"})
    assert merged["me"] == merged["track-b"] == ((160000, 164800),)


def test_turn_gaps_read_session_tracks_or_else_the_files_x_tracks():
    gap = [dict(atMs=10000, durationMs=300, frames=4800, kind="jump")]
    stored = dict(
        id="b",
        kind="remote",
        sourceTrackId="b",
        spans=[dict(file="track-b.wav", sampleRate=16000, frames=480000, startOffsetMs=0)],
        gaps=gap,
    )
    x = {"codemyriad.io": {"tracks": [stored]}}
    assert turn_gaps((), {}, x) == {"track-b": ((160000, 164800),)}
    merges = dict(speakerMerges=[{"from": "track-b", "into": "me"}])
    assert turn_gaps((), merges, x, ["me", "track-b"])["me"] == ((160000, 164800),)
    assert turn_gaps((), {}, {"codemyriad.io": {"tracks": [dict(stored, spans="bad")]}}) == {}
    assert turn_gaps((), {}, {}) == turn_gaps((), {}, None) == {}
