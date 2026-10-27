# SPDX-License-Identifier: GPL-3.0-or-later
import json
from datetime import UTC, datetime

import pytest
from test_library import capture, received, write_metadata

from cassini_viewer import portable, ux_formats
from cassini_viewer.library import scan_library


def test_legacy_viewer_only_opens_the_default_embedded_body(tmp_path):
    directory = received(tmp_path, "016-two-transcripts.opus")
    metadata = json.loads((directory / "meeting.json").read_text())
    original = directory / "original.opus"
    assert scan_library(tmp_path).meetings[0].legacy_transcript_file == original
    embedded = portable.read_meeting(original)
    other = next(t.id for t in embedded.transcripts if t.id != embedded.default_transcript_id)
    metadata["transcript"]["reference"]["transcriptId"] = other
    (directory / "meeting.json").write_text(json.dumps(metadata))
    meeting = scan_library(tmp_path).meetings[0]
    assert meeting.words and meeting.legacy_transcript_file is None


def test_folder_and_received_sidecars_are_not_sent_to_sidecar_unaware_viewer(tmp_path):
    session = capture(tmp_path)
    directory = received(tmp_path, "002-plain-audio.opus")
    for folder in (session.directory, directory):
        value = (
            json.loads((folder / "meeting.json").read_text())
            if (folder / "meeting.json").exists()
            else write_metadata(folder)
        )
        value["transcript"] = dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        )
        (folder / "meeting.json").write_text(json.dumps(value))
        (folder / "transcript.json").write_text(
            json.dumps(dict(items=[dict(speaker="one", startMs=0, endMs=10, text="A readable local transcript.")]))
        )
    meetings = scan_library(tmp_path).meetings
    assert len(meetings) == 2 and all(m.words and m.legacy_transcript_file is None for m in meetings)


@pytest.mark.parametrize(
    "source,expected",
    [
        (None, "UTC"),
        ("", "UTC"),
        ("bad-zone", "UTC"),
        (":/usr/share/zoneinfo/America/New_York", "America/New_York"),
        (":Europe/Rome", "Europe/Rome"),
    ],
)
def test_timezone_identifiers_do_not_fall_back_to_rome(source, expected):
    assert ux_formats.local_timezone(source) == expected


def test_numeric_local_offset_is_used_for_rows_and_day_groups():
    zone = ux_formats.local_timezone("-05:00")
    at = int(datetime(2026, 10, 27, 1, tzinfo=UTC).timestamp() * 1000)
    assert ux_formats.row_time(at, tz=zone) == "20:00"
    assert ux_formats.day_group(at, now_ms=at + 6 * 3600000, tz=zone) == "Yesterday"
