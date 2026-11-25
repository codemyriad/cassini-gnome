# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""D112 (L-87) on published Library files: projection into the file, no rename, export keeps the date."""

import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication tests require GStreamer", allow_module_level=True)

from library_v2_support import simple_session

from cassini_talk import portable
from cassini_talk.library import LibraryScanner
from cassini_talk.library_v2 import finalize, xschema
from cassini_talk.meeting_date import set_started_at
from cassini_talk.meeting_time import milliseconds

SET = "2026-11-23T14:36:00+01:00"


def audio_digest(path):
    with Path(path).open("rb") as stream:
        return portable.compute_audio_digest(stream)["sha256"]


def test_published_file_carries_the_date_without_rename_and_export_keeps_it(tmp_path, monkeypatch):
    from cassini_talk.exporting import export_meeting
    from cassini_talk.open_meeting import load_open_meeting

    sid, directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    original = tmp_path / result.name
    before = portable.read_meeting(str(original))
    digest = audio_digest(original)
    set_started_at(directory, SET)
    assert sorted(p.name for p in tmp_path.glob("*.opus")) == [result.name]  # never renamed
    after = portable.read_meeting(str(original))
    assert after.tags["DATE"] == SET
    assert after.manifest["meeting"]["startedAt"] == SET
    assert after.manifest["meeting"]["recordedAtLocal"] == "2026-11-23T14:36:00"
    assert after.manifest["meeting"]["recordedAtUtc"] == "2026-11-23T13:36:00Z"
    assert after.manifest["meeting"]["createdAtUtc"] == before.manifest["meeting"]["createdAtUtc"]
    ours = after.manifest["x"][xschema.DOMAIN]
    assert ours["meeting"]["startedAt"] == SET and ours["meeting"]["startedAtSource"] == "user"
    assert ours["namePrefix"] == before.manifest["x"][xschema.DOMAIN]["namePrefix"]
    assert audio_digest(original) == digest
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.started_at_ms == milliseconds(SET) and meeting.started_at_user == SET
    # Exported as .opus and opened elsewhere, the meeting keeps the date.
    exported = export_meeting(meeting, tmp_path / "Documents", tz="Asia/Tokyo")
    shared = portable.read_meeting(str(exported))
    assert shared.tags["DATE"] == SET and shared.manifest["meeting"]["startedAt"] == SET
    assert shared.recorded_at_ms == milliseconds(SET)
    assert load_open_meeting(exported).started_at_ms == milliseconds(SET)


def test_unknown_start_library_file_has_no_date_tag(tmp_path):
    sid, directory = simple_session(tmp_path, 0.1)
    session = json.loads((directory / "session.json").read_text())
    session.pop("createdAt", None)
    (directory / "session.json").write_text(json.dumps(session))
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    embedded = portable.read_meeting(str(tmp_path / result.name))
    assert "DATE" not in embedded.tags and embedded.recorded_at_ms is None
