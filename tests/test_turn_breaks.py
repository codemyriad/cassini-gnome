# SPDX-License-Identifier: GPL-3.0-or-later
"""D63 #2: a turn never spans a gap range of its own track, in all three groupers.

Cases come from the vendored protocol vectors (conformance/multistream
writedown.json `turns`); corpus turnCases run in test_corpus_pipelines.
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from cassini_talk.export_model import markdown
from cassini_talk.library import turn_gaps
from cassini_talk.meeting_text import reading_turns
from cassini_talk.portable import Word
from cassini_talk.source_ownership import speaker_gap_ranges, turn_break
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


def test_unreadable_x_track_ids_cost_only_the_breaks():
    # Two spans of one source with ids R64 cannot order: no breaks, never an error.
    spans = [dict(file="a.wav", sampleRate=16000, frames=16000, startOffsetMs=0)]
    for ids in ((1, 2), (None, "b")):
        stored = [dict(kind="browser", sourceTrackId="s", spans=spans, gaps=[]) for _ in ids]
        for row, identifier in zip(stored, ids, strict=True):
            if identifier is not None:
                row["id"] = identifier
        assert turn_gaps((), {}, {"codemyriad.io": {"tracks": stored}}) == {}


JUMP = [dict(atMs=10000, durationMs=300, frames=4800, kind="jump")]


def stored_track(identifier, *, gaps=JUMP, **span):
    span = dict(dict(file=f"{identifier}.wav", sampleRate=16000, frames=480000, startOffsetMs=0), **span)
    return dict(id=identifier, kind="browser", sourceTrackId="s", spans=[span], gaps=gaps)


# Malformed foreign x tracks; most share the good track's source, so R64 orders them together.
MALFORMED = {
    "zero rate": stored_track("bad", sampleRate=0, gaps=[]),
    "float rate": stored_track("bad", sampleRate=16000.0, gaps=[]),
    "text frames": stored_track("bad", frames="480000", gaps=[]),
    "text offset": stored_track("bad", startOffsetMs="0", gaps=[]),
    "boolean offset": stored_track("bad", startOffsetMs=True, gaps=[]),
    "no rate": dict(stored_track("bad", gaps=[]), spans=[dict(file="bad.wav", frames=16000, startOffsetMs=0)]),
    "anchored zero rate": stored_track("bad", sampleRate=0, clockAnchors=[[0, 0], [16000, 10**9]], gaps=[]),
    "nan offset": stored_track("bad", startOffsetMs=float("nan"), gaps=[]),
    # Valid anchors whose 48 kHz positions collapse to one float: mapped_end_sample divided by zero.
    "collapsed anchors": stored_track("bad", sampleRate=1, clockAnchors=[[2**62, 0], [2**62 + 1, 10**9]], gaps=[]),
    # A finite offset whose 48 kHz origin is inf: round(NaN).
    "huge anchored offset": stored_track("bad", startOffsetMs=1e307, clockAnchors=[[0, 0], [16000, 10**9]], gaps=[]),
    # json.loads accepts a lone surrogate, which R64's UTF-8 sort keys cannot encode.
    "surrogate id": dict(stored_track("bad", gaps=[]), id="\ud800"),
    "surrogate source": dict(stored_track("bad", gaps=[]), sourceTrackId="\ud800"),
    "surrogate own source": dict(stored_track("bad", gaps=[]), sourceTrackId="\udfff"),
}
# A good track on another source, which the bad rows must not cost its breaks.
OTHER = dict(stored_track("other", gaps=[dict(atMs=30000, durationMs=1000, frames=0, kind="jump")]), sourceTrackId="o")
KEPT = {"track-s": ((160000, 164800),), "track-o": ((480000, 496000),)}


@pytest.mark.parametrize("bad", MALFORMED.values(), ids=MALFORMED)
def test_a_malformed_x_track_costs_only_its_own_breaks(bad):
    # Codex #182: a sampleRate 0 span sharing a source raised ZeroDivisionError.
    for tracks in ([stored_track("good"), bad, OTHER], [bad, OTHER, stored_track("good")]):
        assert turn_gaps((), {}, {"codemyriad.io": {"tracks": tracks}}) == KEPT


def test_a_source_whose_overlap_lane_id_is_too_long_costs_only_its_own_breaks():
    # R64 refuses a lane id over 256 characters; only that source's rows go.
    long = "l" * 250
    tracks = [dict(stored_track(name), sourceTrackId=long) for name in ("l1", "l2")]
    for rows in ([*tracks, stored_track("good"), OTHER], [OTHER, stored_track("good"), *tracks]):
        assert turn_gaps((), {}, {"codemyriad.io": {"tracks": rows}}) == KEPT
    # Separated one source at a time, lanes keep the ids of one R64 pass.
    shared = [stored_track("a"), stored_track("b"), dict(stored_track("c"), sourceTrackId="s:overlap:1")]
    gaps = turn_gaps((), {}, {"codemyriad.io": {"tracks": shared}})
    assert sorted(gaps) == ["track-s", "track-s:overlap:1", "track-s:overlap:2"]


def test_one_bad_x_track_row_is_skipped_and_the_others_keep_their_breaks():
    # Codex #182: one non-string id emptied the whole map.
    other = dict(stored_track("b", gaps=[dict(atMs=30000, durationMs=1000, frames=0, kind="jump")]), sourceTrackId="b")
    bad = [dict(stored_track("x"), id=7), "row", None, dict(stored_track("y"), spans="bad")]
    rows = [bad[0], stored_track("good"), *bad[1:], other]
    gaps = turn_gaps((), {}, {"codemyriad.io": {"tracks": rows}})
    assert gaps == {"track-s": ((160000, 164800),), "track-b": ((480000, 496000),)}
    assert grouped("track-s", words_of("track-s", [(9700, 9950), (10300, 10600)]), gaps) == ([1, 1],) * 3


def foreign_file(path, tracks):
    """A conformance file whose x["codemyriad.io"] carries `tracks` (another writer's)."""
    from test_library import ROOT

    from cassini_talk import portable
    from cassini_talk.library_v2 import edit

    source = ROOT / "tests/conformance/opus/001-minimal-v1.opus"
    manifest = portable.read_meeting(str(source)).manifest
    manifest.setdefault("x", {})["codemyriad.io"] = {"tracks": tracks}
    replacements, _ = edit.chunk_set(edit.MANIFEST_PREFIX, manifest, edit.MANIFEST_MIME)
    with open(source, "rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    edit.rewrite(source, path, edit.edited_fields(fields, replacements), vendor)
    return path


def test_a_malformed_foreign_file_keeps_its_meeting_the_scan_and_its_good_breaks(tmp_path, monkeypatch):
    # Codex #182, end to end: opened directly, as a Received folder and as a root file (v2 index).
    import hashlib
    from types import SimpleNamespace
    from uuid import uuid4

    from test_library import write_metadata

    from cassini_talk import portable_writer
    from cassini_talk.library import LibraryScanner
    from cassini_talk.open_meeting import load_open_meeting

    # Another writer's ASCII JSON can carry a lone surrogate as an escape; ours writes UTF-8.
    ascii_json = SimpleNamespace(dumps=lambda value, **options: json.dumps(value, **dict(options, ensure_ascii=True)))
    monkeypatch.setattr(portable_writer, "json", ascii_json)

    expected = KEPT
    for name, bad in MALFORMED.items():
        if name == "nan offset":
            continue  # JSON in a file has no NaN
        tracks = [stored_track("good"), bad, dict(stored_track("x"), id=7), OTHER]
        opened = load_open_meeting(foreign_file(tmp_path / f"{uuid4()}.opus", tracks))
        assert opened.transcript == "done" and opened.turn_gaps == expected, name
        # One root each: root files of the same audio are one row.
        root = tmp_path / str(uuid4())
        folder = root / f"Received-{uuid4()}"
        folder.mkdir(parents=True)
        original = foreign_file(folder / "original.opus", tracks)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        received = dict(originalName=original.name, sha256=digest, importedAtMs=1793107080000)
        reference = dict(kind="embedded", file=original.name)
        write_metadata(folder, transcript=dict(status="done", reference=reference), received=received)
        foreign_file(root / "Foreign.opus", tracks)
        meetings = LibraryScanner(root).scan().meetings
        assert sorted(m.directory.name for m in meetings) == ["Foreign.opus", folder.name], name
        assert all(m.transcript == "done" and m.turn_gaps == expected for m in meetings), name


def test_the_files_own_silent_tracks_apply_to_its_x_tracks():
    # The window, open view and a Received folder have no sender meeting.json;
    # the file's x meeting lists its D56 silent tracks.
    def stored(identifier, start, ms, at, duration):
        return dict(
            id=identifier,
            kind="browser",
            sourceTrackId="s",
            gaps=[dict(atMs=at, durationMs=duration, kind="jump", frames=0)],
            spans=[dict(file=f"{identifier}.wav", sampleRate=16000, frames=ms * 16, startOffsetMs=start)],
        )

    details = dict(recordingDetails=dict(writingDown=dict(silentTracks=["a"])))
    x = {"codemyriad.io": {"tracks": [stored("a", 0, 30000, 5000, 3000), stored("b", 20000, 40000, 40000, 5000)]}}
    assert turn_gaps((), details, x) == {"track-s": ((640000, 720000),)}
    x["codemyriad.io"]["meeting"] = details
    for metadata in ({}, dict(title="Received", speakers=[])):
        gaps = turn_gaps((), metadata, x)
        assert gaps == {"track-s": ((640000, 720000),)}
        assert grouped("track-s", words_of("track-s", [(38000, 39000), (46000, 46500)]), gaps) == ([1, 1],) * 3
        assert grouped("track-s", words_of("track-s", [(4000, 4500), (9000, 9500)]), gaps) == ([2],) * 3


def test_a_merge_into_a_voice_keeps_its_groups_ranges():
    # The merged-away track has ranges of its own, so the voice gets a direct entry.
    rows = [
        track("sys", kind="system", source=None, gaps=[dict(atMs=10000, durationMs=5000, frames=0, kind="jump")]),
        track("b", source="b", gaps=[dict(atMs=30000, durationMs=1000, frames=0, kind="jump")]),
    ]
    gaps = speaker_gap_ranges(rows, merges={"track-b": "others~1"})
    across_others, across_b = [(9000, 9500), (16000, 16500)], [(29000, 29500), (31500, 32000)]
    for speaker in ("others~1", "others~2"):
        assert grouped(speaker, words_of(speaker, across_others), gaps) == ([1, 1],) * 3
    assert grouped("others~1", words_of("others~1", across_b), gaps) == ([1, 1],) * 3
    assert grouped("others~2", words_of("others~2", across_b), gaps) == ([2],) * 3


def test_merged_ranges_are_joined_without_changing_a_break():
    def jump(at, duration):
        return dict(atMs=at, durationMs=duration, frames=0, kind="jump")

    gaps, other = [jump(1000, 2000), jump(6000, 1000)], [jump(2500, 1500), jump(7000, 500)]
    rows = [track("mic", kind="microphone", source=None, gaps=gaps), track("b", source="b", gaps=other)]
    merged = speaker_gap_ranges(rows, merges={"track-b": "me"})["me"]
    assert merged == ((16000, 64000), (96000, 112000), (112000, 120000))
    raw = [(16000, 48000), (40000, 64000), (96000, 112000), (112000, 120000)]
    edges = range(0, 9000, 250)
    for first in edges:
        for second in edges:
            low, high = sorted((16 * first, 16 * second))
            expected = any(left < high and low < right for left, right in raw)
            assert turn_break(merged, (first, first), (second, second)) is expected


def test_a_break_looks_up_a_few_ranges_not_all_of_them():
    # Long recordings keep many gaps; window._set_words groups on the GTK main thread.
    class Counted(Sequence):
        def __init__(self, values):
            self.values, self.reads = values, 0

        def __len__(self):
            return len(self.values)

        def __getitem__(self, index):
            self.reads += 1
            return self.values[index]

    ranges = Counted(tuple((32000 * i, 32000 * i + 1600) for i in range(1, 4097)))
    assert not turn_break(ranges, (4096 * 2000 + 200, 4096 * 2000 + 300), (4096 * 2000 + 400, 4096 * 2000 + 500))
    assert turn_break(ranges, (1900, 1950), (2150, 2200))
    assert ranges.reads <= 40
