# SPDX-License-Identifier: GPL-3.0-or-later
"""D112 (L-87) Set date and time: stored field, reader precedence, file projection, export and prefill."""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from cassini_talk import portable
from cassini_talk.library import LibraryScanner
from cassini_talk.library_v2 import xschema
from cassini_talk.meeting_date import changes, prefill, saved_ms, set_started_at, started_at
from cassini_talk.meeting_store import read_metadata
from cassini_talk.meeting_time import milliseconds, selected
from cassini_talk.received import import_received

ROOT = Path(__file__).resolve().parents[1]
ROUND_TRIP = ROOT / "ux/library-vectors/round-trip"
ASSETS = ROOT / "ux/library-vectors/assets"
NOW = 1795600000000  # 2026-11-25
SET = "2026-11-23T14:36:00+01:00"


def audio_digest(path):
    with Path(path).open("rb") as stream:
        return portable.compute_audio_digest(stream)["sha256"]


@pytest.mark.parametrize(
    ("vector", "start_ms", "wall"),
    [("user-started-at", 1795440960000, "2026-11-23T14:36:00"), ("legacy-import-time-date", None, None)],
)
def test_shared_round_trip_vectors_resolve_the_start(vector, start_ms, wall):
    """cassini-ux D112 vectors: the person's date wins; a legacy import-time DATE is never the start."""
    expected = json.loads((ROUND_TRIP / f"{vector}.expected.json").read_text())
    embedded = portable.read_meeting(str(ROUND_TRIP / f"{vector}.opus"))
    ours = embedded.manifest["x"][xschema.DOMAIN]
    assert xschema.valid(ours)
    assert xschema.rebuild(ours, None, session_exists=False) == expected["rebuiltMeetingJson"]
    assert embedded.recorded_at_ms == start_ms and embedded.recorded_at_local == wall
    assert (start_ms is None) == expected["inDateUnknownGroup"]


def test_reader_precedence_user_start_wins_and_import_time_excludes_embedded_dates():
    manifest = dict(meeting=dict(createdAtUtc="2026-11-24T08:15:00Z", recordedAtUtc="2026-11-01T10:00:00Z"))
    assert selected(manifest, {})["source"] == "recordedAtUtc"
    manifest["x"] = {xschema.DOMAIN: dict(namePrefixSource="import-time", meeting={})}
    assert selected(manifest, {"DATE": "2026-11-01T11:00:00+01:00"}) is None
    manifest["x"][xschema.DOMAIN]["meeting"] = dict(startedAt=SET, startedAtSource="user")
    assert selected(manifest, {}) == dict(source="startedAt", value=SET)
    # A manifest meeting.startedAt (A4) from another app is read as the start.
    assert selected(dict(meeting=dict(createdAtUtc="2026-11-24T08:15:00Z", startedAt=SET)), {})["value"] == SET


def test_started_at_uses_the_days_own_offset_and_floors_to_the_minute():
    assert started_at(2026, 11, 23, 14, 36, tz="Europe/Rome") == SET
    assert started_at(2026, 7, 1, 9, 5, tz="Europe/Rome") == "2026-07-01T09:05:00+02:00"
    assert started_at(2026, 7, 1, 9, 5, tz="UTC") == "2026-07-01T09:05:00+00:00"
    assert started_at(2026, 1, 2, 3, 4, tz="America/St_Johns") == "2026-01-02T03:04:00-03:30"


def test_prefill_order_recording_time_then_saved_time_minus_length_then_now():
    class Row:
        started_at_ms = None
        recorded_at_local = None
        duration_ms = 36 * 60 * 1000
        started_at_user = None

    row = Row()
    saved = milliseconds("2026-11-23T15:12:00+01:00")
    assert prefill(row, tz="Europe/Rome", now_ms=NOW, saved=saved).at == datetime(2026, 11, 23, 14, 36)
    assert prefill(row, tz="Europe/Rome", now_ms=NOW, saved=saved).from_file
    now = prefill(row, tz="Europe/Rome", now_ms=NOW)
    assert now.at == datetime(2026, 11, 25, 10, 46) and not now.from_file
    for implausible in (0, NOW + 2 * 86_400_000):
        assert prefill(row, tz="Europe/Rome", now_ms=NOW, saved=implausible) == now
    row.recorded_at_local = "2026-10-01T09:30:00"
    assert prefill(row, tz="Europe/Rome", now_ms=NOW, saved=saved).at == datetime(2026, 10, 1, 9, 30)
    row.started_at_ms = milliseconds("2026-11-20T08:15:42Z")
    value = prefill(row, tz="Europe/Rome", now_ms=NOW, saved=saved)
    assert value.at == datetime(2026, 11, 20, 9, 15) and not value.from_file


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    root.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return root


def test_imported_meeting_without_a_date_moves_to_its_day_and_date_unknown_disappears(library):
    source = ASSETS / "plain-title.opus"
    before = source.read_bytes()
    received = import_received(source, library)
    # The copy keeps the original's last-saved time, for the prefill only.
    assert os.stat(received.audio_file).st_mtime_ns == os.stat(source).st_mtime_ns
    assert saved_ms(received) == os.stat(source).st_mtime_ns // 1_000_000
    snapshot = LibraryScanner(library).scan()
    assert [g.title for g in snapshot.groups(now_ms=NOW, tz="Europe/Rome")] == ["Date unknown"]
    audio = audio_digest(received.audio_file)
    assert changes(snapshot.meetings[0], SET)
    saved = set_started_at(received.directory, SET)
    assert saved["startedAt"] == SET and saved["startedAtSource"] == "user"
    (meeting,) = LibraryScanner(library).scan().meetings
    assert meeting.started_at_ms == milliseconds(SET) and meeting.started_at_user == SET
    assert not changes(meeting, SET)
    groups = LibraryScanner(library).scan().groups(now_ms=NOW, tz="Europe/Rome")
    assert [g.title for g in groups] == ["Monday 23 November"]
    assert audio_digest(meeting.audio_file) == audio and source.read_bytes() == before
    assert read_metadata(meeting.directory)["received"] == received.metadata["received"]
    # The same value again is a no-op.
    assert set_started_at(received.directory, SET) == saved


def test_set_date_rejects_values_outside_the_contract(library):
    received = import_received(ASSETS / "plain-title.opus", library)
    for value in ("2026-11-23T14:36:00", "2026-11-23T14:36:00-00:00", "2026-11-23T14:36:00.5+01:00", "soon"):
        with pytest.raises(ValueError):
            set_started_at(received.directory, value)
    assert "startedAt" not in read_metadata(received.directory)
