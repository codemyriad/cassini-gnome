# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure Python root-file readers, also run in the minimal Library gate."""

import shutil
from pathlib import Path

from cassini_viewer import portable
from cassini_viewer.library import LibraryScanner

ASSETS = Path(__file__).parent / "conformance/opus"


def root_file(root, name="Meeting.opus", fixture="021-descriptors-first.opus"):
    target = root / name
    shutil.copyfile(ASSETS / fixture, target)
    return target


def test_root_file_without_private_metadata_has_audio_identity_and_readable_words(tmp_path):
    path = root_file(tmp_path)
    with path.open("rb") as stream:
        digest = portable.compute_audio_digest(stream)["sha256"]
    before = path.read_bytes(), path.stat().st_mtime_ns
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.id == digest and meeting.origin == "received"
    assert meeting.key == "file/" + digest
    assert meeting.audio_file == path and meeting.words and meeting.transcript == "done"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not (tmp_path / ".cassini").exists()
    path.rename(tmp_path / "Renamed externally.opus")
    (renamed,) = LibraryScanner(tmp_path).scan().meetings
    assert renamed.id == digest and renamed.key == meeting.key


def test_reader_never_follows_a_hidden_metadata_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    library = tmp_path / "Library"
    library.mkdir()
    path = root_file(library)
    (library / ".cassini").symlink_to(outside, target_is_directory=True)
    snapshot = LibraryScanner(library).scan()
    assert len(snapshot.meetings) == 1
    assert snapshot.meetings[0].audio_file == path and not snapshot.meetings[0].metadata_editable
    assert any(d.code == "unsafe-sessions-directory" for d in snapshot.diagnostics)
    assert list(outside.iterdir()) == []


def test_private_stage_symlink_and_directory_are_not_root_files(tmp_path):
    for name in (".private.opus", "unfinished.opus.stage", "unfinished.partial"):
        (tmp_path / name).write_bytes(b"bad")
    (tmp_path / "directory.opus").mkdir()
    (tmp_path / "linked.opus").symlink_to(tmp_path / ".private.opus")
    assert not LibraryScanner(tmp_path).scan().meetings
    broken = tmp_path / "broken.opus"
    broken.write_bytes(b"bad")
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.published_file == broken and meeting.transcript == "damaged"
    assert not meeting.has_audio and not meeting.metadata_editable


def test_bad_payload_is_visible_but_cannot_be_edited(tmp_path):
    path = root_file(tmp_path, fixture="009-bad-payload-sha256.opus")
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.audio_file == path and meeting.transcript == "damaged"
    assert not meeting.metadata_editable and not meeting.can_write_down
    assert not (tmp_path / ".cassini").exists()
