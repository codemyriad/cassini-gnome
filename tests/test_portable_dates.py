# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 DATE is recording provenance; CREATED_AT always describes this export."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cassini_viewer.library import Meeting
from cassini_viewer.portable_writer import descriptors, recording_date

DIGEST = dict(sha256="a" * 64, channels=1, sampleCount=48000, durationMs=1000)


def test_capture_site_offset_survives_a_different_export_timezone():
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
    before = datetime.now(UTC)
    tags, manifest = descriptors(meeting, DIGEST, tz="America/New_York")
    after = datetime.now(UTC)
    assert tags["DATE"] == "2026-10-31T07:50:07+05:45"
    assert manifest["meeting"]["recordedAtLocal"] == "2026-10-31T07:50:07"
    assert (
        before.timestamp() - 0.001
        <= datetime.fromisoformat(tags["CASSINI_CREATED_AT"]).timestamp()
        <= after.timestamp()
    )
    assert tags["CASSINI_CREATED_AT"] == manifest["meeting"]["createdAtUtc"]
    assert tags["DATE"] != tags["CASSINI_CREATED_AT"]


def test_legacy_capture_without_an_offset_uses_the_export_timezone():
    meeting = Meeting(
        duration_ms=1000,
        id="legacy",
        directory=Path("Recording-legacy"),
        title="Legacy",
        origin="own",
        started_at_ms=1730611800999,
    )
    tags, manifest = descriptors(meeting, DIGEST, tz="America/New_York")
    assert tags["DATE"] == "2024-11-03T01:30:00-04:00"
    assert manifest["meeting"]["recordedAtLocal"] == "2024-11-03T01:30:00"


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
    assert tags["DATE"] == "2024-11-03T01:30:00-04:00"
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
        "2026-10-31T02:00:00+14:01",
        "2026-10-31T02:00:00-12:01",
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
