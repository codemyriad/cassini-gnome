# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 DATE is recording provenance; CREATED_AT always describes this export."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cassini_talk.library import Meeting
from cassini_talk.portable_writer import descriptors, recording_date

DIGEST = dict(sha256="a" * 64, channels=1, sampleCount=48000, durationMs=1000)


def test_capture_site_offset_survives_a_different_export_timezone(sealed_at):
    instant = datetime(2026, 10, 31, 2, 5, 7, 999000, UTC)
    meeting = Meeting(
        duration_ms=1000,
        id="recording",
        directory=Path("Recording-source"),
        title="Site time",
        origin="own",
        started_at_ms=int(instant.timestamp() * 1000),
        recording_offset_minutes=345,
    )
    tags, manifest = descriptors(meeting, DIGEST, tz="America/New_York")
    assert tags["DATE"] == tags["CASSINI_RECORDED_AT_UTC"] == "2026-10-31T02:05:07.999000Z"
    assert manifest["meeting"]["recordedAtLocal"] == "2026-10-31T07:50:07"
    assert datetime.fromisoformat(tags["CASSINI_CREATED_AT"]) == sealed_at
    assert tags["CASSINI_CREATED_AT"] == manifest["meeting"]["createdAtUtc"]
    assert tags["DATE"] != tags["CASSINI_CREATED_AT"]


def test_legacy_capture_without_site_offset_writes_utc_without_inventing_site_label():
    meeting = Meeting(
        duration_ms=1000,
        id="legacy",
        directory=Path("Recording-legacy"),
        title="Legacy",
        origin="own",
        started_at_ms=1730611800999,
    )
    tags, manifest = descriptors(meeting, DIGEST, tz="America/New_York")
    assert tags["DATE"] == "2024-11-03T05:30:00.999000Z"
    assert "recordedAtLocal" not in manifest["meeting"]


@pytest.mark.parametrize("origin", ["own", "received"])
def test_file_reexport_preserves_absolute_date_and_only_explicit_wall_label(origin):
    meeting = Meeting(
        duration_ms=1000,
        id="file",
        directory=Path("working"),
        title="File",
        origin=origin,
        audio_file=Path("meeting.opus"),
        published_file=Path("meeting.opus"),
        started_at_ms=1730611800000,
    )
    tags, manifest = descriptors(meeting, DIGEST, tz="Asia/Tokyo", recorded_date="2024-11-03T01:30:00.999-04:00")
    assert tags["DATE"] == "2024-11-03T05:30:00.999000Z"
    assert "recordedAtLocal" not in manifest["meeting"]
    tags, manifest = descriptors(
        meeting,
        DIGEST,
        tz="Asia/Tokyo",
        recorded_date="2024-11-03T01:30:00-04:00",
        recorded_at_local="2024-11-03T01:30:00",
    )
    assert manifest["meeting"]["recordedAtLocal"] == "2024-11-03T01:30:00"


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "2026",
        "2026-10-31",
        "2026-10-31T02:00:00",
        "2026-10-31T02:00:00-00:00",
        "2026-10-31T02:00:00+00:60",
        "2026-13-31T02:00:00Z",
        "2026-10-31 02:00:00Z",
    ],
)
def test_incomplete_unknown_or_invalid_date_never_invents_recording_provenance(invalid):
    assert recording_date(invalid) is None
    meeting = Meeting(
        duration_ms=1000,
        id="received",
        directory=Path("Received-file"),
        title="File",
        origin="received",
        started_at_ms=1730611800000,
    )
    tags, manifest = descriptors(meeting, DIGEST, recorded_date=invalid, tz="Asia/Tokyo")
    assert tags["DATE"] == tags["CASSINI_CREATED_AT"]
    assert "recordedAtLocal" not in manifest["meeting"]


@pytest.mark.parametrize(
    "value,utc",
    [
        ("2026-10-31T02:00:00+14:01", "2026-10-30T11:59:00Z"),
        ("2026-10-31T02:00:00-12:01", "2026-10-31T14:01:00Z"),
    ],
)
def test_d95_valid_explicit_offset_is_an_instant_even_outside_site_offset_limits(value, utc):
    # D95/RFC3339 DATE fallback is broader than the capture site's offset field.
    meeting = Meeting(
        duration_ms=1000,
        id="file",
        directory=Path("Received-file"),
        title="File",
        origin="received",
        started_at_ms=None,
    )
    tags, manifest = descriptors(meeting, DIGEST, recorded_date=value)
    assert tags["DATE"] == tags["CASSINI_RECORDED_AT_UTC"] == manifest["meeting"]["recordedAtUtc"] == utc
    assert "recordedAtLocal" not in manifest["meeting"]
