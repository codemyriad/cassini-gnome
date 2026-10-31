# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the reader against the format's published conformance vectors.

The vectors and index.json are vendored from cassini-format's
spec/conformance/ (CC0-1.0).
"""

import json
from pathlib import Path

import pytest

from cassini_talk import portable
from cassini_talk.portable import State

HERE = Path(__file__).parent / "conformance"
INDEX = json.loads((HERE / "index.json").read_text())
VECTORS = {v["id"]: v for v in INDEX["vectors"]}

# States a reader that has not looked at the audio can reach on its own.
METADATA_STATES = {State.PLAIN_AUDIO, State.UNKNOWN_FORMAT, State.INVALID, State.UNVERIFIED}


def observe(path: Path):
    meeting = portable.read_meeting(str(path))
    word_counts, unavailable = {}, {}
    for entry in meeting.transcripts:
        try:
            word_counts[entry.id] = len(portable.load_words(meeting, entry.id))
        except portable.CassiniError as exc:
            unavailable[entry.id] = exc.code
    check = portable.verify_audio(str(path), meeting)
    return meeting, word_counts, unavailable, check


@pytest.mark.parametrize("vector_id", sorted(VECTORS))
def test_vector(vector_id):
    vector = VECTORS[vector_id]
    expect = vector["expect"]
    meeting, word_counts, unavailable, check = observe(HERE / vector["file"])

    expected = State(expect["state"])
    if expected in (State.OK, State.STALE_AUDIO):
        assert meeting.state == State.UNVERIFIED
        assert check.state == expected
    elif expected == State.UNVERIFIED and expect.get("audioTrust") == "unverified":
        # 011: the tag and the manifest disagree on the digest, so nothing can be verified.
        assert check.state == State.UNVERIFIED
    else:
        assert meeting.state == expected
        if expected == State.UNVERIFIED:
            # The metadata-profile expectation; a full reader may then reach ok.
            assert check.state in (State.UNVERIFIED, State.OK)

    if not meeting.is_readable:
        assert meeting.manifest is None
        return
    if "transcriptIds" in expect:
        assert [e.id for e in meeting.transcripts] == expect["transcriptIds"]
    if "defaultTranscriptId" in expect:
        assert meeting.default_transcript_id == expect["defaultTranscriptId"]
    if "wordCounts" in expect:
        word_ids = {e.id for e in meeting.transcripts if e.format == portable.WORDS_FORMAT}
        assert {k: v for k, v in word_counts.items() if k in word_ids} == expect["wordCounts"]
    if "speakerIds" in expect:
        assert [s.id for s in meeting.speakers] == expect["speakerIds"]
    if "audioOpusSha256" in expect and check.sha256:
        assert check.sha256 == expect["audioOpusSha256"]
    for error in expect.get("errors", []):
        if error["code"] == "chunk-missing":
            assert "chunk-missing" in unavailable.values()
    for warning in expect.get("warnings", []):
        if warning["code"] == "tag-manifest-disagreement" and warning["tag"].startswith("CASSINI_TX"):
            assert any(n.tag == warning["tag"] for n in meeting.warnings)


def test_plain_audio_digest_matches_multichunk():
    """002 and 003 share their audio: the digest must not depend on the tags."""
    with open(HERE / "opus/002-plain-audio.opus", "rb") as a, open(HERE / "opus/003-multichunk-v1.opus", "rb") as b:
        plain, multi = portable.compute_audio_digest(a), portable.compute_audio_digest(b)
    assert plain["sha256"] == multi["sha256"] == VECTORS["002-plain-audio"]["expect"]["audioOpusSha256"]


def test_ogg_crc_matches_reference():
    """The page CRC is not zlib's CRC-32; check the fast path against a bitwise one."""

    def slow(data):
        crc = 0
        for byte in data:
            crc ^= byte << 24
            for _ in range(8):
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        return crc

    for sample in (b"", b"OggS", bytes(range(256)) * 3):
        assert portable.ogg_crc(sample) == slow(sample)
