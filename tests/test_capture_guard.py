# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved-on-exit intent uses the same metadata transaction as the queue."""

import pytest
from test_library import capture, write_metadata

from cassini_viewer.capture_guard import queue_saved_for_later
from cassini_viewer.meeting_store import LOCK, read_metadata
from cassini_viewer.recording_session import RecordingSession
from cassini_viewer.transcription import REFERENCE


def test_close_save_does_not_queue_empty_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("mic", "Me", "microphone", 48000, 1)
    session.finish()
    queue_saved_for_later(session.directory)
    assert read_metadata(session.directory)["transcript"]["status"] == "none"


def test_close_save_reads_and_updates_under_queue_lock(tmp_path, monkeypatch):
    from cassini_viewer import meeting_store

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status="failed", reference=REFERENCE))
    original = meeting_store.read_metadata
    reads = []

    def locked_read(directory):
        assert LOCK._is_owned(), "Read-modify-write must share the transcription lock"
        reads.append(directory)
        return original(directory)

    monkeypatch.setattr(meeting_store, "read_metadata", locked_read)
    queue_saved_for_later(session.directory)
    result = original(session.directory)
    assert reads and result["transcript"] == dict(status="queued", reference=REFERENCE)
    assert result["error"] is None and result["etaMinutes"] is None


def test_close_save_never_overwrites_newer_running_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status="running", reference=REFERENCE))
    before = (session.directory / "meeting.json").read_bytes()
    queue_saved_for_later(session.directory)
    assert (session.directory / "meeting.json").read_bytes() == before


def test_concurrent_browser_stop_is_already_successful(tmp_path, monkeypatch):
    from cassini_viewer import browser_service
    from cassini_viewer.capture_guard import stop_browser_captures

    path = str(tmp_path / "Recording-one")
    active = True

    def request(action="status", **values):
        nonlocal active
        if action == "stop":
            active = False
            raise ValueError("This meeting is not recording")
        return dict(statuses=[dict(id="one:2", path=path, recording=active)], meetings=[])

    monkeypatch.setattr(browser_service, "request", request)
    assert stop_browser_captures() == [path]


@pytest.mark.parametrize("meeting_present", [True, False])
def test_stop_waits_for_an_existing_browser_save(tmp_path, monkeypatch, meeting_present):
    from cassini_viewer import browser_service
    from cassini_viewer.capture_guard import stop_browser_captures

    path = str(tmp_path / "Recording-one")
    attempts = []

    def request(action="status", **values):
        if action == "stop":
            attempts.append(values["id"])
            if len(attempts) == 1:
                raise ValueError("A recording command is still being processed")
        active = len(attempts) < 2
        return dict(statuses=[dict(id="one:2", path=path, recording=active)], meetings=[dict(id="one:2", busy=active)])

    monkeypatch.setattr(browser_service, "request", request)
    assert stop_browser_captures() == [path]
    assert len(attempts) == 2
