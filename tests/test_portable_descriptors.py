# SPDX-License-Identifier: GPL-3.0-or-later
"""Required manifest descriptors are the record, unlike optional/copy tags."""

import hashlib
import json
import struct
from pathlib import Path

import pytest
from test_library import received

from cassini_talk import portable
from cassini_talk.library import scan_library

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "tests/conformance/opus/001-minimal-v1.opus"
REQUIRED = ("ENCODING", "CHUNK_COUNT", "SHA256", "RAW_BYTES", "GZIP_BYTES", "MIME", "SCHEMA")


@pytest.mark.parametrize("suffix", REQUIRED)
@pytest.mark.parametrize("empty", [False, True], ids=["missing", "empty"])
def test_manifest_requires_each_descriptor_even_with_decodable_payload(suffix, empty):
    head, comments, vendor = portable.parse_header(REFERENCE.read_bytes())
    tag = "CASSINI_PAYLOAD_" + suffix
    assert dict(comments)[tag]
    altered = (
        [(name, "" if name == tag else value) for name, value in comments]
        if empty
        else [(name, value) for name, value in comments if name != tag]
    )
    meeting = portable.interpret(head, altered, vendor)
    assert meeting.state == portable.State.INVALID and meeting.manifest is None
    assert not meeting.transcripts and not meeting.is_readable
    assert any(n.code == "payload-descriptor-incomplete" and n.tag == tag for n in meeting.warnings)


def test_copies_and_optional_summary_tags_do_not_become_required_descriptors():
    head, comments, vendor = portable.parse_header(REFERENCE.read_bytes())
    # The manifest is the record for these copies; missing copies must not
    # reject an otherwise readable body (SPEC "Which declaration wins").
    copies = {
        "CASSINI_PROFILE",
        "CASSINI_TRANSCRIPT_IDS",
        "CASSINI_TRANSCRIPT_DEFAULT",
        "CASSINI_DECODE_HINT",
        "CASSINI_MEETING_ID",
        "CASSINI_CREATED_AT",
        "CASSINI_SPEAKER_COUNT",
    }
    comments = [
        (name, value) for name, value in comments if name not in copies and not name.startswith("CASSINI_AUDIO_")
    ]
    meeting = portable.interpret(head, comments, vendor)
    assert meeting.state == portable.State.UNVERIFIED
    assert len(portable.load_words(meeting, meeting.default_transcript_id)) == 3


def without_tag(data, tag):
    """Rewrite only the fixture's one-page OpusTags; keep every audio page."""

    def page_end(start):
        segments = data[start + 26]
        return start + 27 + segments + sum(data[start + 27 : start + 27 + segments])

    first_end = page_end(0)
    second_end = page_end(first_end)
    _, comments, vendor = portable.parse_header(data)
    comments = [(name + "=" + value).encode() for name, value in comments if name != tag]
    vendor = vendor.encode()
    packet = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    packet += b"".join(struct.pack("<I", len(value)) + value for value in comments)
    lacing = bytes([255] * (len(packet) // 255) + [len(packet) % 255])
    assert len(lacing) <= 255 and data[first_end + 5] == 0
    page = bytearray(data[first_end : first_end + 27])
    page[22:26], page[26] = bytes(4), len(lacing)
    page += lacing + packet
    struct.pack_into("<I", page, 22, portable.ogg_crc(page))
    return data[:first_end] + bytes(page) + data[second_end:]


@pytest.mark.parametrize("suffix", ["MIME", "SCHEMA"])
def test_missing_manifest_descriptor_damages_received_body_but_keeps_audio(tmp_path, suffix):
    directory = received(tmp_path)
    original = directory / "original.opus"
    asset = ROOT / "ux/library-vectors/assets/onboarding.opus"
    original.write_bytes(without_tag(asset.read_bytes(), "CASSINI_PAYLOAD_" + suffix))
    metadata = json.loads((directory / "meeting.json").read_text())
    metadata["received"]["sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    (directory / "meeting.json").write_text(json.dumps(metadata))
    before = original.read_bytes()
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.transcript == "damaged" and meeting.error.id == "transcript-damaged"
    assert meeting.error.action is None and meeting.badge().kind == "received"
    assert meeting.duration_ms == 2040000 and meeting.audio_file == original and meeting.has_audio
    assert meeting.started_at_ms == metadata["received"]["importedAtMs"]
    assert meeting.quote_line is None and not meeting.speakers and not meeting.can_write_down
    assert original.read_bytes() == before
    with asset.open("rb") as positive, original.open("rb") as negative:
        assert portable.compute_audio_digest(positive) == portable.compute_audio_digest(negative)
