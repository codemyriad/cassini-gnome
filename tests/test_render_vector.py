# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""D84: unmodified output of the pinned web viewer, without GTK or a simulator."""

import json
from pathlib import Path

import pytest

from cassini_talk.meeting_text import reading_turns
from cassini_talk.portable import Word

VECTORS = Path(__file__).parent / "ux" / "interjections"


def members(row):
    result = []
    chips = {}
    for after, aside in row.asides:
        chips.setdefault(after, []).append(aside)
    pending = []

    def flush():
        if pending:
            result.append(dict(kind="words", wordIndices=list(pending)))
            pending.clear()

    for offset, index in enumerate(row.word_indices):
        pending.append(index)
        for aside in chips.get(offset, []):
            flush()
            result.append(
                dict(
                    kind="interjection",
                    speaker=aside.speaker,
                    crosstalk=aside.crosstalk,
                    wordIndices=aside.word_indices,
                )
            )
    flush()
    return result


def coalesce(members):
    """The vector permits concatenating adjacent speech members, not chips."""
    output = []
    for member in members:
        member = {key: value for key, value in member.items() if key != "text"}
        if output and member["kind"] == output[-1]["kind"] == "words":
            output[-1]["wordIndices"] = output[-1]["wordIndices"] + member["wordIndices"]
        else:
            output.append(member)
    return output


@pytest.mark.parametrize("path", sorted(VECTORS.glob("*.expected.json")), ids=lambda path: path.stem)
def test_viewer_rows(path):
    vector = json.loads(path.read_text())
    body = json.loads(path.with_name(vector["fixture"]).read_text())
    words = [
        Word(
            item["speaker"],
            item["startMs"],
            item["endMs"],
            item["text"],
            item.get("lowConfidenceSpeaker", False),
            text_is_stored=True,
        )
        for item in body["items"]
    ]
    actual = [
        dict(
            speaker=row.speaker,
            continuation=row.continuation,
            startMs=row.start_ms,
            endMs=row.end_ms,
            over=row.over,
            crosstalk=row.crosstalk,
            members=members(row),
        )
        for row in reading_turns(words)
    ]
    expected = [dict(row, members=coalesce(row["members"])) for row in vector["rows"]]
    assert actual == expected
    # Every atom survives exactly once, including folded words; text stays stored.
    indices = [i for row in actual for member in row["members"] for i in member["wordIndices"]]
    assert sorted(indices) == list(range(len(words)))
    assert [word.text for word in words] == [item["text"] for item in body["items"]]


def test_inflated_end_repair_uses_reference_words_and_provenance():
    from dataclasses import replace

    from cassini_talk.readable import repair_word_ends

    words = [Word("a", i * 500, i * 500 + 400, "word") for i in range(8)]
    words += [Word("a", 4000, 9000, "Yes"), Word("a", 9000, 9000, '."')]
    repaired = repair_word_ends(words, [list(range(10))])
    assert repaired[8].end_ms == 5600  # 4 × 400 ms; punctuation excluded
    assert words[8].end_ms == 9000
    bounded = [replace(word, ends_bounded_by_audio=True) for word in words]
    assert repair_word_ends(bounded, [list(range(10))]) == bounded
    # Sparse speakers fall back to the meeting reference, not their inflated end.
    words[-2:] = [Word("b", 4000, 9000, "Yes."), Word("b", 9000, 9000, "”")]
    assert repair_word_ends(words, [list(range(8)), [8, 9]])[8].end_ms == 5600


def test_continuation_has_sixty_second_boundary_and_keeps_timestamp():
    words = [Word("a", 0, 400, "First"), Word("a", 60400, 60800, "Second"), Word("a", 120801, 121201, "Third")]
    rows = reading_turns(words)
    assert [row.continuation for row in rows] == [False, True, False]
    assert [row.start_ms for row in rows] == [0, 60400, 120801]


def test_overlap_order_excludes_chips_and_names_largest_stretch_first():
    words = [
        Word("a", 0, 4000, "holding"),
        Word("b", 100, 500, "short"),
        Word("c", 200, 2200, "longer"),
        Word("d", 300, 1300, "middle"),
    ]
    rows = reading_turns(words)
    assert rows[0].over == ["c", "d", "b"]
    assert not any(row.asides for row in rows)  # no split host seam


@pytest.mark.parametrize("path", sorted(VECTORS.glob("*.expected.json")), ids=lambda path: path.stem)
def test_vector_prose_in_previews_search_and_markdown(path, tmp_path):
    from cassini_talk.export_model import markdown
    from cassini_talk.library import LibraryScanner
    from cassini_talk.search_index import search_turns

    expected = json.loads(path.read_text())
    body = json.loads(path.with_name(expected["fixture"]).read_text())
    words = tuple(Word(i["speaker"], i["startMs"], i["endMs"], i["text"], text_is_stored=True) for i in body["items"])
    prose = [
        " ".join(m["text"] if m["kind"] == "words" else f"({m['speaker']}: {m['text']})" for m in row["members"])
        for row in expected["rows"]
    ]
    previews = LibraryScanner(tmp_path)._preview("vector", words)
    assert [row.text for row in previews] == prose[:3]
    assert [row.text for row in search_turns(words, {})] == prose
    speakers = [dict(id=speaker, name=speaker) for speaker in dict.fromkeys(w.speaker for w in words)]
    exported = markdown(dict(title="vector", items=body["items"], speakers=speakers, verbatim=True))
    lines = exported.split("## Transcript\n", 1)[1].splitlines()
    assert [line for line in lines if line and not line.startswith("**")] == prose
    headers = [line for line in lines if line.startswith("**")]
    assert [" (over " in line for line in headers] == [bool(row["over"]) for row in expected["rows"]]


@pytest.mark.parametrize("bounded,body_flag", [(True, False), (False, True), (None, True)])
def test_decoder_preserves_audio_bound_provenance(tmp_path, bounded, body_flag):
    from dataclasses import replace

    from cassini_talk import portable, portable_writer

    original = Path(__file__).parent / "conformance" / "opus" / "001-minimal-v1.opus"
    source = portable.read_meeting(str(original))
    manifest = json.loads(json.dumps(source.manifest))
    manifest["speakers"] = [dict(id="a", label="Ada")]
    if bounded is not None:
        manifest["provenance"] = dict(wordTimings=dict(endsBoundedByAudio=bounded))
    else:
        manifest.pop("provenance", None)
    entry = next(t for t in manifest["transcripts"] if t["id"] == source.default_transcript_id)
    body = dict(
        format=portable.WORDS_FORMAT,
        wordCount=1,
        items=[dict(speaker="a", startMs=0, endMs=5000, text="Yes.")],
        provenance=dict(wordTimings=dict(endsBoundedByAudio=body_flag)),
    )
    tags = dict(source.tags)
    transcript_tags, entry["payloadRef"] = portable_writer.chunk_set(
        entry["payloadRef"]["prefix"], body, portable_writer.WORDS_MIME
    )
    entry["wordCount"] = 1
    tags.update(transcript_tags)
    tags.update(portable_writer.chunk_set("CASSINI_PAYLOAD_", manifest, portable_writer.MANIFEST_MIME)[0])
    path = tmp_path / "bounded.opus"
    with original.open("rb") as stream, path.open("wb") as output:
        portable_writer.rewrite_tags(stream, output, tags)
    meeting = portable.read_meeting(str(path))
    assert meeting.is_readable
    words = portable.load_words(meeting, meeting.default_transcript_id)
    assert words[0].ends_bounded_by_audio is (bounded is True)
    assert words[0] != replace(words[0], ends_bounded_by_audio=not words[0].ends_bounded_by_audio)
    assert reading_turns(words)[0].end_ms == (5000 if bounded is True else 1000)


def test_stored_whitespace_and_quotes_are_not_rewritten():
    from cassini_talk.transcript import row_text

    word = Word("a", 0, 400, '  "word"\t', text_is_stored=True)
    (row,) = reading_turns([word])
    assert row.items == (word.text,)
    assert row_text(row)[0] == word.text


def test_folded_search_hit_seeks_the_reply_not_the_host(tmp_path):
    from dataclasses import replace

    from cassini_talk.library import LibraryScanner, LibrarySnapshot
    from cassini_talk.search_index import SearchIndex

    path = VECTORS / "acknowledgment.words.json"
    items = json.loads(path.read_text())["items"]
    words = tuple(Word(i["speaker"], i["startMs"], i["endMs"], i["text"], text_is_stored=True) for i in items)
    # Reuse the existing fast Library fixture rather than opening any UI.
    from test_library import capture, write_metadata

    session = capture(tmp_path / "Library")
    write_metadata(session.directory)
    (meeting,) = LibraryScanner(session.directory.parent).scan().meetings
    meeting = replace(meeting, words=words, transcript="done")
    index = SearchIndex(session.directory.parent, tmp_path / "cache")
    try:
        index.sync(LibrarySnapshot(meetings=(meeting,)))
        (hit,) = index.query("good", now_ms=0, tz="UTC")
        assert hit.position_ms == next(word.start_ms for word in words if word.text == "good.")
        assert hit.speaker == next(word.speaker for word in words if word.text == "good.")
        assert "(ben: Sounds " in hit.marked
    finally:
        index.close()


def test_folded_reply_speaker_filter_keeps_context_and_seek(tmp_path):
    from dataclasses import replace

    from test_library import capture, write_metadata

    from cassini_talk.library import LibraryScanner, LibrarySnapshot, LibrarySpeaker
    from cassini_talk.search_index import SearchIndex

    session = capture(tmp_path / "Library")
    write_metadata(session.directory)
    (meeting,) = LibraryScanner(session.directory.parent).scan().meetings
    words = (Word("a", 0, 1000, "Before"), Word("b", 900, 1100, "Reply"), Word("a", 1000, 2000, "after"))
    speakers = tuple(LibrarySpeaker(s, name, "named", 0, i) for i, (s, name) in enumerate((("a", "Ada"), ("b", "Ben"))))
    meeting = replace(meeting, words=words, speakers=speakers, transcript="done")
    index = SearchIndex(session.directory.parent, tmp_path / "cache")
    try:
        index.sync(LibrarySnapshot(meetings=(meeting,)))
        (hit,) = index.query("Reply", speaker="Ben", now_ms=0, tz="UTC")
        assert hit.position_ms == 900
        assert hit.speaker == "b"
        assert hit.marked == "Before (Ben: \x01Reply\x02) after"
        assert index.query("Reply", speaker="Ada", now_ms=0, tz="UTC") == ()
        assert index.query("Before", speaker="Ben", now_ms=0, tz="UTC") == ()
        assert index.query("Ben", now_ms=0, tz="UTC") == ()  # chip label is not spoken text
        assert len(index.query("Reply", now_ms=0, tz="UTC")) == 1
    finally:
        index.close()


def test_native_highlight_uses_repaired_end_instead_of_the_inflated_tail():
    from cassini_talk.meeting_text import reading_markup, reading_word

    word = Word("guest", 188000, 199000, "September?", text_is_stored=True)
    (row,) = reading_turns([word])
    assert row.end_ms == 189000
    assert "background=" in reading_markup(row, 188500, str.upper)
    assert "background=" not in reading_markup(row, 192000, str.upper)
    assert reading_word(row, "item:0").start_ms == 188000
    assert word.end_ms == 199000  # display repair never rewrites source data


def test_interleaved_listening_fixture_has_sixteen_viewer_rows():
    words = []
    for index in range(127):
        words.extend(
            (
                Word("host", index * 150, index * 150 + 140, "Planning."),
                Word("voice-0", index * 150 + 140, index * 150 + 141, "Yes."),
            )
        )
    words.append(Word("voice-1", 20500, 20600, "Ready."))
    words.extend(Word(f"voice-{i}", 22000 + i * 1000, 22100 + i * 1000, "Yes.") for i in range(13))
    rows = reading_turns(words)
    assert len(rows) == 16
    assert [len(row.words) for row in rows[:2]] == [127, 127]
    assert not any(row.asides for row in rows)
    assert [row.speaker for row in rows[2:]] == ["voice-1", *[f"voice-{i}" for i in range(13)]]


CORPUS_VECTOR = Path(__file__).parent / "ux" / "interjections-corpus"


def projected_row(row):
    return dict(
        speaker=row.speaker,
        continuation=row.continuation,
        startMs=row.start_ms,
        endMs=row.end_ms,
        over=row.over,
        crosstalk=row.crosstalk,
        members=members(row),
    )


def assert_corpus_interjection_row(rows, *, exact=True):
    """Independent viewer oracle for the physical 49–50 s overlap."""
    expected = json.loads((CORPUS_VECTOR / "corpus-accept-all.expected.json").read_text())["rows"]
    expected = [
        dict(row, members=coalesce(row["members"]))
        for row in expected
        if row["speaker"] == "track-source-a" and row["startMs"] < 50000 and row["endMs"] > 49000
    ]
    assert len(expected) == 1
    actual = [
        projected_row(row)
        for row in rows
        if row.speaker == "track-source-a" and row.start_ms < 50000 and row.end_ms > 49000
    ]
    if not exact:
        # The live acoustic fixture already checks its timestamp tolerances.
        # Pin classification and visible member roles here, while frozen-word
        # tests below compare every timestamp and original atom index exactly.
        def roles(row):
            return dict(
                speaker=row["speaker"],
                over=row["over"],
                crosstalk=row["crosstalk"],
                members=[{k: v for k, v in member.items() if k != "wordIndices"} for member in row["members"]],
            )

        actual, expected = [roles(row) for row in actual], [roles(row) for row in expected]
    assert actual == expected


def corpus_vector_words():
    body = json.loads((CORPUS_VECTOR / "corpus-accept-all.words.json").read_text())
    return [
        Word(
            item["speaker"],
            item["startMs"],
            item["endMs"],
            item["text"],
            item.get("lowConfidenceSpeaker", False),
            text_is_stored=True,
        )
        for item in body["items"]
    ]


def test_actual_corpus_words_match_the_complete_viewer_portable_projection():
    expected = json.loads((CORPUS_VECTOR / "corpus-accept-all.expected.json").read_text())["rows"]
    actual = [projected_row(row) for row in reading_turns(corpus_vector_words())]
    assert actual == [dict(row, members=coalesce(row["members"])) for row in expected]


def test_actual_corpus_interjection_matches_viewer_with_source_gap_information():
    gaps = json.loads((CORPUS_VECTOR / "source-gaps.json").read_text())
    assert_corpus_interjection_row(reading_turns(corpus_vector_words(), gaps))


COMPARISON = json.loads((Path(__file__).parent / "ux/interjections-review.json").read_text())["cases"]


@pytest.mark.parametrize("case", COMPARISON, ids=lambda case: case["name"])
def test_seeded_zero_duration_and_punctuation_comparison_with_viewer(case):
    words = [Word(i["speaker"], i["startMs"], i["endMs"], i["text"], text_is_stored=True) for i in case["items"]]
    rows = reading_turns(words)
    actual = [
        dict(
            speaker=row.speaker,
            continuation=row.continuation,
            startMs=row.start_ms,
            endMs=row.end_ms,
            over=row.over,
            crosstalk=row.crosstalk,
            members=members(row),
        )
        for row in rows
    ]
    assert actual == [dict(row, members=coalesce(row["members"])) for row in case["rows"]]
    assert sorted(i for row in rows for m in members(row) for i in m["wordIndices"]) == list(range(len(words)))


def test_follow_scroll_uses_sounding_blocks_in_reading_order():
    from cassini_talk.meeting_text import FollowRows
    from cassini_talk.transcript import Turn

    ada = Turn(0, "a", [Word("a", 0, 60000, "holding")], 0, 0, 60000)
    ben = Turn(1, "b", [Word("b", 10000, 14000, "competing")], 1, 10000, 14000)
    follow = FollowRows([ada, ben])
    assert follow.at(12000) == 0
    assert follow.at(50000) == 0
    assert follow.at(60000) == -1
    # A's paragraphs stay together even when their starts cross B's row.
    tail = Turn(1, "a", [Word("a", 30000, 60000, "tail")], 1, 30000, 60000)
    follow = FollowRows([Turn(0, "a", [Word("a", 0, 5000, "first")], 0, 0, 5000), tail, ben])
    assert follow.at(12000) == 2
    assert follow.at(50000) == 1
    # A long silent hole must not hide sounding B; small within-block gaps bridge.
    ada.words = [Word("a", 0, 1000, "first"), Word("a", 1300, 1500, "next"), Word("a", 50000, 60000, "last")]
    follow = FollowRows([ada, ben])
    assert follow.at(1150) == 0
    assert follow.at(12000) == 1
    assert follow.at(20000) == -1


def test_folded_chip_follow_and_preview_rename_reuse_the_model(tmp_path):
    from cassini_talk.library import LibraryScanner
    from cassini_talk.meeting_text import FollowRows

    words = (Word("a", 0, 1000, "Before"), Word("b", 900, 1100, "Reply"), Word("a", 1000, 2000, "after"))
    scanner = LibraryScanner(tmp_path)
    first = scanner._preview("m", words, speaker_names={"a": "Ada", "b": "Ben"})
    rows = scanner._preview_cache["m"][2]
    assert first[0].text == "Before (Ben: Reply) after"
    assert FollowRows(rows).at(1050) == 0
    second = scanner._preview("m", words, speaker_names={"a": "Ada", "b": "Bea"})
    assert second[0].text == "Before (Bea: Reply) after"
    assert scanner._preview_cache["m"][2] is rows


def test_interrupted_long_turn_visits_timing_pools_linearly(monkeypatch):
    from cassini_talk import transcript

    visits = [0]
    original = transcript._spans

    class Pool(list):
        def __getitem__(self, index):
            visits[0] += 1
            return super().__getitem__(index)

    monkeypatch.setattr(transcript, "_spans", lambda words: Pool(original(words)))
    words = []
    for i in range(256):
        start = i * 3300
        words.extend((Word("a", start, start + 3100, "holding"), Word("b", start + 3100, start + 3200, "reply")))
    turns = transcript.build_turns(words)
    assert len(turns) == 257
    assert sum(len(turn.asides) for turn in turns) == 255
    assert not any(turn.over for turn in turns)
    assert turns[0].word_indices == list(range(0, len(words), 2))
    assert [(after, aside.word_indices) for after, aside in turns[0].asides] == [(i, [2 * i + 1]) for i in range(255)]
    assert visits[0] < 20 * len(words), "long host timing pools were rescanned for each reply"
