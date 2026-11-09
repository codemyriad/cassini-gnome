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


def test_decoder_preserves_audio_bound_provenance(monkeypatch):
    from cassini_talk import portable

    path = Path(__file__).parent / "conformance" / "opus" / "001-minimal-v1.opus"
    meeting = portable.read_meeting(str(path))
    item = dict(speaker="a", startMs=0, endMs=5000, text="Yes.")
    body = dict(items=[item], provenance=dict(wordTimings=dict(endsBoundedByAudio=True)))
    monkeypatch.setattr(portable, "decode_chunk_set", lambda *_a, **_kw: body)
    words = portable.load_words(meeting, meeting.default_transcript_id)
    assert words[0].ends_bounded_by_audio
    assert reading_turns(words)[0].end_ms == 5000
    body["provenance"] = None
    words = portable.load_words(meeting, meeting.default_transcript_id)
    assert not words[0].ends_bounded_by_audio
    assert reading_turns(words)[0].end_ms == 1000


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
        assert "(ben: Sounds " in hit.marked
    finally:
        index.close()
