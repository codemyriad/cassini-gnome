# SPDX-License-Identifier: GPL-3.0-or-later
"""Every readable meeting projection carries the speaker playback model."""

import pytest
from published_listening_support import published_opus

from cassini_talk.library import LibraryScanner
from cassini_talk.meeting_store import update_metadata
from cassini_talk.open_meeting import load_open_meeting
from cassini_talk.received import import_received


@pytest.mark.parametrize("merged", [False, True])
def test_root_published_file_carries_effective_listening_turns_without_a_working_session(tmp_path, merged):
    path = published_opus(tmp_path, merges=[{"from": "guest", "into": "silvio"}] if merged else [])
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.published_file == path and meeting.has_audio
    assert not meeting.directory.exists(), "the file-only projection must suffice"
    assert [(t.speaker, t.start_ms, t.end_ms) for t in meeting.listening_turns] == (
        [("silvio", 2000, 13000)] if merged else [("silvio", 2000, 8000), ("guest", 9000, 13000)]
    )
    assert tuple(i for turn in meeting.listening_turns for i in turn.word_indices) == (0, 1)


def test_external_and_received_files_also_carry_listening_turns_and_local_merges(tmp_path, monkeypatch):
    path = published_opus(tmp_path / "External")
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    opened = load_open_meeting(path)
    assert not opened.metadata_editable and opened.has_audio
    assert [t.speaker for t in opened.listening_turns] == ["silvio", "guest"]
    received = import_received(path)
    assert received.origin == "received" and received.has_audio
    assert [t.speaker for t in received.listening_turns] == ["silvio", "guest"]
    update_metadata(received.directory, speakerMerges=[{"from": "guest", "into": "silvio"}])
    (merged,) = LibraryScanner(root).scan().meetings
    assert [(t.speaker, t.start_ms, t.end_ms) for t in merged.listening_turns] == [("silvio", 2000, 13000)]
