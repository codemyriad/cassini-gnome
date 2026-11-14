# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - optional encoder/runtime dependencies precede product imports.
"""Library v2 Move to Trash / Restore on real published files (S-12 §7.1, D100, D-1032).

Cases (a) published file only and (b) published file plus working folder, through the app's
own scanner rows and identity reader (our x), plus a crash between the two moves.
"""

import os
import shutil

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("Managed Trash integration runs with the full app runtime", allow_module_level=True)

from library_v2_support import simple_session

from cassini_talk.library import LibraryScanner
from cassini_talk.library_v2 import finalize, journal
from cassini_talk.library_v2 import trash as storage
from cassini_talk.meeting_trash import trash_meeting


class Crash(BaseException):
    pass


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    root.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sid, directory = simple_session(root, 0.1)
    result = finalize.finalize(root, sid)
    return root, sid, directory, root / result.name


def rows(root):
    return LibraryScanner(root).scan().meetings


def test_published_file_and_working_folder_move_together_and_come_back_whole(managed):
    root, sid, directory, file = managed
    original = file.read_bytes()
    (meeting,) = rows(root)
    assert meeting.published_file == file and meeting.directory == directory
    _root, entry_id = trash_meeting(meeting)
    entry = root / ".cassini/trash" / entry_id
    assert (entry / "meeting.opus").read_bytes() == original, "no byte of the meeting is rewritten (D51)"
    assert (entry / directory.name / "meeting.json").exists()
    assert not file.exists() and not directory.exists() and not rows(root)
    (row,) = storage.entries(root)
    assert row.identity == {"session": f"Recording-{sid}"} and row.title == "Weekly sync"
    assert storage.restore(root, entry_id) == file.name
    assert file.read_bytes() == original and directory.is_dir()
    (back,) = rows(root)
    assert back.published_file == file and back.directory == directory and back.title == "Weekly sync"


def test_published_file_without_working_folder_is_case_a(managed):
    root, sid, directory, file = managed
    shutil.rmtree(directory)
    (meeting,) = rows(root)
    _root, entry_id = trash_meeting(meeting)
    assert sorted(os.listdir(root / ".cassini/trash" / entry_id)) == ["entry.json", "meeting.opus"]
    assert not rows(root)
    storage.restore(root, entry_id)
    assert [m.published_file for m in rows(root)] == [file]


def test_restore_takes_a_suffix_when_another_meeting_has_the_name(managed):
    root, sid, directory, file = managed
    (meeting,) = rows(root)
    _root, entry_id = trash_meeting(meeting)
    sid2, _directory2 = simple_session(root, 0.1, created="2026-10-30T17:02:41Z")
    second = finalize.finalize(root, sid2)
    assert second.name == file.name  # the same minute and title took the free name
    restored = storage.restore(root, entry_id)
    assert restored == file.name.replace(".opus", " (2).opus")
    assert {m.title for m in rows(root)} == {"Weekly sync"} and len(rows(root)) == 2


def test_restore_refused_while_a_copy_with_the_same_identity_is_live(managed):
    root, sid, directory, file = managed
    original = file.read_bytes()
    (meeting,) = rows(root)
    _root, entry_id = trash_meeting(meeting)
    (root / "copied back by hand.opus").write_bytes(original)
    with pytest.raises(storage.Refused, match="already in the Library"):
        storage.restore(root, entry_id)
    assert [e.entry_id for e in storage.entries(root)] == [entry_id]
    assert not journal.pending(journal.Layout(root)).entries


def test_crash_between_the_two_moves_never_shows_a_split_meeting(managed, monkeypatch):
    root, sid, directory, file = managed
    (meeting,) = rows(root)
    calls = []

    def checkpoint(count):
        calls.append(count)
        if len(calls) == 5:  # journal, mkdir trash, mkdir entry, entry.json, file moved
            raise Crash()

    monkeypatch.setattr(storage, "CHECKPOINT", checkpoint)
    with pytest.raises(Crash):
        trash_meeting(meeting)
    monkeypatch.setattr(storage, "CHECKPOINT", None)
    assert not file.exists() and directory.exists()
    assert not rows(root), "a pending Move to Trash hides every representation"
    report = journal.recover(root)
    assert len(report.completed) == 1 and not report.kept
    assert not directory.exists() and len(storage.entries(root)) == 1 and not rows(root)


def test_purge_removes_every_representation(managed):
    root, sid, directory, file = managed
    (meeting,) = rows(root)
    _root, entry_id = trash_meeting(meeting)
    storage.purge(root, entry_id)
    assert not os.listdir(root / ".cassini/trash") and not rows(root)
    assert not file.exists() and not directory.exists()
