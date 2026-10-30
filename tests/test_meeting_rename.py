# SPDX-License-Identifier: GPL-3.0-or-later
"""Rename preserves concurrent job metadata, private values and original audio."""

import hashlib
from pathlib import Path

import pytest

from cassini_viewer.library import LibraryScanner
from cassini_viewer.meeting_rename import rename_meeting, title_value
from cassini_viewer.meeting_store import read_metadata, update_metadata
from cassini_viewer.received import import_received
from cassini_viewer.recording_session import RecordingSession


@pytest.fixture
def meeting(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    session = RecordingSession(None, title="Captured title", source="gnome")
    session.add_track("mic", "Me", "microphone", 16000, 1)
    session.append("mic", b"\x01\x00" * 48000, 0)
    session.finish()
    update_metadata(session.directory, notes=[dict(atMs=20, text="Private note")], future=dict(keep=[1, 2]))
    return LibraryScanner(root).scan().meetings[0]


def bytes_except_metadata(meeting):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in meeting.directory.iterdir()
        if p.name != "meeting.json"
    }


def test_rename_trims_and_preserves_notes_private_values_source_and_folder(meeting):
    before = read_metadata(meeting.directory)
    audio = bytes_except_metadata(meeting)
    renamed = rename_meeting(meeting.directory, "  Weekly sync  ")
    assert renamed == dict(before, title="Weekly sync", titleSource="user", revision=before["revision"] + 1)
    assert bytes_except_metadata(meeting) == audio
    assert LibraryScanner(meeting.directory.parent).scan().meetings[0].title == "Weekly sync"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_rename_while_writing_down_keeps_current_progress_and_metadata(meeting, status):
    update_metadata(
        meeting.directory, transcript=dict(status=status, reference=None), etaMinutes=3, processing=dict(future="kept")
    )
    before = read_metadata(meeting.directory)
    renamed = rename_meeting(meeting.directory, "New name")
    assert renamed == dict(before, title="New name", titleSource="user", revision=before["revision"] + 1)


def test_received_rename_changes_only_local_metadata(meeting):
    source = Path(__file__).parents[1] / "ux/library-vectors/assets/plain-title.opus"
    before = source.read_bytes()
    received = import_received(source, meeting.directory.parent)
    provenance = dict(received.metadata["received"])
    audio = bytes_except_metadata(received)
    saved = rename_meeting(received.directory, "Received name")
    assert saved["title"] == "Received name" and saved["titleSource"] == "user"
    assert saved["received"] == provenance and bytes_except_metadata(received) == audio
    assert source.read_bytes() == before


def test_empty_is_explicit_untitled_and_never_reveals_captured_title(meeting):
    saved = rename_meeting(meeting.directory, " \t ")
    assert saved["title"] == "Untitled meeting" and saved["titleSource"] == "user"
    assert LibraryScanner(meeting.directory.parent).scan().meetings[0].title == "Untitled meeting"


def test_unchanged_title_does_not_publish_a_revision(meeting):
    before = (meeting.directory / "meeting.json").read_bytes()
    rename_meeting(meeting.directory, "  Captured title  ")
    assert (meeting.directory / "meeting.json").read_bytes() == before


@pytest.mark.parametrize("invalid", [None, "x" * 501, "\ud800"])
def test_invalid_title_never_changes_metadata(meeting, invalid):
    before = (meeting.directory / "meeting.json").read_bytes()
    with pytest.raises(ValueError):
        rename_meeting(meeting.directory, invalid)
    assert (meeting.directory / "meeting.json").read_bytes() == before
    assert title_value("名" * 500) == "名" * 500


def test_failed_atomic_save_keeps_the_previous_name_and_no_temporary_file(meeting, monkeypatch):
    before = (meeting.directory / "meeting.json").read_bytes()
    original = Path.replace

    def failed(path, target):
        if Path(target) == meeting.directory / "meeting.json":
            raise PermissionError("disconnected destination")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", failed)
    with pytest.raises(PermissionError):
        rename_meeting(meeting.directory, "New name")
    assert (meeting.directory / "meeting.json").read_bytes() == before
    assert not list(meeting.directory.glob(".meeting.json-*"))


def test_outside_and_live_meetings_cannot_be_renamed(meeting, tmp_path):
    import shutil

    outside = tmp_path / "Outside" / meeting.directory.name
    outside.parent.mkdir()
    shutil.copytree(meeting.directory, outside)
    before = (outside / "meeting.json").read_bytes()
    with pytest.raises(ValueError, match="outside"):
        rename_meeting(outside, "Outside changed")
    assert (outside / "meeting.json").read_bytes() == before
    live = RecordingSession(None, title="Still recording", source="gnome")
    try:
        with pytest.raises(ValueError, match="recording"):
            rename_meeting(live.directory, "Not allowed")
        assert not (live.directory / "meeting.json").exists()
    finally:
        live.finish()
