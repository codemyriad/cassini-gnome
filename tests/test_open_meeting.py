# SPDX-License-Identifier: GPL-3.0-or-later
import time

from test_library import ROOT, capture, fingerprint

from cassini_viewer.open_meeting import load_open_meeting
from cassini_viewer.transcription import TranscriptionQueue


def test_external_folder_is_read_only_and_queue_enforces_library_boundary(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    root.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    session = capture(tmp_path)
    before = fingerprint(session.directory)
    meeting = load_open_meeting(session.directory / "session.json")
    assert meeting.has_audio and not meeting.metadata_editable and not meeting.can_write_down
    finished = []
    queue = TranscriptionQueue(lambda _: None, finished.append, bundle=tmp_path)
    try:
        assert queue.enqueue(session.directory)
        deadline = time.monotonic() + 3
        while not finished and time.monotonic() < deadline:
            time.sleep(0.005)
        assert finished == [session.directory]
        assert fingerprint(session.directory) == before
        assert not (session.directory / "meeting.json").exists()
    finally:
        queue.close()


def test_library_session_open_is_editable_and_does_not_scan_siblings(tmp_path, monkeypatch):
    from cassini_viewer.library import LibraryScanner

    session, neighbor = capture(tmp_path), capture(tmp_path)
    seen = []
    read = LibraryScanner._read

    def observed(self, fd, directory, *args):
        seen.append(directory)
        return read(self, fd, directory, *args)

    monkeypatch.setattr(LibraryScanner, "_read", observed)
    meeting = load_open_meeting(session.directory / "session.json", tmp_path)
    assert meeting.metadata_editable
    assert seen == [session.directory] and neighbor.directory not in seen


def test_damaged_embedded_body_remains_on_reading_path_with_playable_audio():
    path = ROOT / "ux/library-vectors/assets/009-bad-payload-sha256.opus"
    meeting = load_open_meeting(path)
    assert meeting.transcript == "damaged" and not meeting.words
    assert meeting.has_audio and not meeting.metadata_editable
