# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - optional encoder/runtime dependencies precede product imports.
"""Library v2 Move to Trash / Restore on real published files (S-12 §7.1, D100, D-1032).

Cases (a) published file only and (b) published file plus working folder, through the app's
own scanner rows and identity reader (our x), plus a crash between the two moves, directory
fsync failures right after a rename, and purges overlapping an unfinished move.
"""

import errno
import os
import shutil
from pathlib import Path

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


# ---- a directory fsync fails after a rename (codex R1) ---------------------------------------


def fail_fsync_after_rename(monkeypatch, rename_number, fsync_number):
    """The `fsync_number`-th directory fsync after the `rename_number`-th rename fails once."""
    state = {"renames": 0, "fsyncs": 0, "failed": False}
    real_native, real_fsync = storage.fsutil.native, storage._fsync_parent

    def native():
        rename = real_native()

        def counted(old, new):
            result = rename(old, new)
            if result[0] == 0:
                state["renames"] += 1
                state["fsyncs"] = 0
            return result

        return counted

    def fsync(path):
        if state["renames"] == rename_number and not state["failed"]:
            state["fsyncs"] += 1
            if state["fsyncs"] == fsync_number:
                state["failed"] = True
                raise OSError(errno.EIO, "generated directory fsync failure")
        real_fsync(path)

    monkeypatch.setattr(storage.fsutil, "native", native)
    monkeypatch.setattr(storage, "_fsync_parent", fsync)
    return state


def pending(root):
    return journal.pending(journal.Layout(root), cleanup=False).entries


@pytest.mark.parametrize("rename_number", [1, 2], ids=["published-file", "working-folder"])
@pytest.mark.parametrize("fsync_number", [1, 2], ids=["source-dir", "target-dir"])
def test_move_fsync_failure_after_a_rename_keeps_entry_and_journal_for_recovery(
    managed, monkeypatch, rename_number, fsync_number
):
    root, sid, directory, file = managed
    original = file.read_bytes()
    (meeting,) = rows(root)
    state = fail_fsync_after_rename(monkeypatch, rename_number, fsync_number)
    with pytest.raises(storage.Failed):
        trash_meeting(meeting)
    assert state["failed"]
    monkeypatch.undo()
    (entry_dir,) = (root / ".cassini/trash").iterdir()
    assert (entry_dir / "entry.json").is_file(), "the recovery information stays"
    assert (entry_dir / "meeting.opus").read_bytes() == original and not file.exists()
    assert [e["kind"] for e in pending(root)] == ["trash"]
    assert not rows(root), "a pending Move to Trash hides every representation"
    report = journal.recover(root)
    assert len(report.completed) == 1 and not report.kept and not pending(root)
    assert not directory.exists() and (entry_dir / directory.name / "meeting.json").exists()
    (row,) = storage.entries(root)
    assert storage.restore(root, row.entry_id) == file.name
    assert file.read_bytes() == original and directory.is_dir()


@pytest.mark.parametrize("rename_number", [1, 2], ids=["working-folder", "published-file"])
@pytest.mark.parametrize("fsync_number", [1, 2], ids=["source-dir", "target-dir"])
def test_restore_fsync_failure_after_a_rename_keeps_entry_and_journal_for_recovery(
    managed, monkeypatch, rename_number, fsync_number
):
    root, sid, directory, file = managed
    original = file.read_bytes()
    (meeting,) = rows(root)
    _root, entry_id = trash_meeting(meeting)
    entry_dir = root / ".cassini/trash" / entry_id
    state = fail_fsync_after_rename(monkeypatch, rename_number, fsync_number)
    with pytest.raises(storage.Failed):
        storage.restore(root, entry_id)
    assert state["failed"]
    monkeypatch.undo()
    assert (entry_dir / "entry.json").is_file()
    assert [e["kind"] for e in pending(root)] == ["restore"]
    report = journal.recover(root)
    assert len(report.completed) == 1 and not report.kept and not pending(root)
    assert not entry_dir.exists() and not storage.entries(root)
    assert file.read_bytes() == original and directory.is_dir()
    (back,) = rows(root)
    assert back.published_file == file and back.directory == directory


def test_purge_fsync_failure_after_its_rename_keeps_the_journal_and_finishes_forward(managed, monkeypatch):
    root, sid, directory, file = managed
    (meeting,) = rows(root)
    _root, entry_id = trash_meeting(meeting)
    state = fail_fsync_after_rename(monkeypatch, 1, 1)
    with pytest.raises(storage.Failed):
        storage.purge(root, entry_id)
    assert state["failed"]
    monkeypatch.undo()
    assert [e["kind"] for e in pending(root)] == ["purge"]
    assert (root / ".cassini/trash" / f".purge-{entry_id}" / "entry.json").is_file()
    report = journal.recover(root)
    assert len(report.completed) == 1 and not report.kept and not pending(root)
    assert not os.listdir(root / ".cassini/trash") and not rows(root)


# ---- a purge never overlaps an unfinished move (codex R1) ------------------------------------


def half_moved(managed, monkeypatch):
    """Move to Trash moved the published file, then the working folder's move failed."""
    root, sid, directory, file = managed
    (meeting,) = rows(root)
    real_rename = storage._rename

    def rename(source, target):
        if Path(source).name == directory.name:
            raise OSError(errno.EIO, "generated working-folder move failure")
        real_rename(source, target)

    monkeypatch.setattr(storage, "_rename", rename)
    with pytest.raises(storage.Failed):
        trash_meeting(meeting)
    (row,) = storage.entries(root)  # listed: entry.json and the published file are there
    assert directory.is_dir() and not file.exists() and [e["kind"] for e in pending(root)] == ["trash"]
    return row.entry_id


def gone_for_good(root, directory, file):
    assert not pending(root)
    report = journal.recover(root)  # a later replay resurrects nothing
    assert not report.completed and not report.kept
    assert not storage.entries(root) and not rows(root)
    assert not os.listdir(root / ".cassini/trash")
    assert not directory.exists() and not file.exists()


def purge_by(how, root, entry_id):
    if how == "delete":
        return storage.purge(root, entry_id)
    if how == "empty":
        return storage.empty_trash(root)
    return storage.expire(root, storage.parse_utc("2099-01-01T00:00:00Z"))


@pytest.mark.parametrize("how", ["delete", "empty", "expire"])
def test_purge_finishes_an_unfinished_move_first_and_nothing_comes_back(managed, monkeypatch, how):
    root, sid, directory, file = managed
    entry_id = half_moved(managed, monkeypatch)
    monkeypatch.undo()  # the working folder can move now: the purge settles the trash op first
    purged = purge_by(how, root, entry_id)
    if how != "delete":
        assert purged == [{"session": f"Recording-{sid}"}]
    gone_for_good(root, directory, file)


@pytest.mark.parametrize("how", ["delete", "empty", "expire"])
def test_purge_refused_while_an_earlier_move_of_the_meeting_cannot_finish(managed, monkeypatch, how):
    root, sid, directory, file = managed
    entry_id = half_moved(managed, monkeypatch)  # the folder still cannot move
    if how == "expire":
        assert purge_by(how, root, entry_id) == [], "expiry skips it until the move finishes"
    else:
        expected = storage.Incomplete if how == "empty" else storage.Refused
        with pytest.raises(expected, match="unfinished|not finished") as raised:
            purge_by(how, root, entry_id)
        if how == "empty":
            assert raised.value.purged == [] and raised.value.kept == [entry_id]
    assert [e.entry_id for e in storage.entries(root)] == [entry_id], "the entry stays"
    assert (root / ".cassini/trash" / entry_id / "meeting.opus").is_file() and directory.is_dir()
    assert [e["kind"] for e in pending(root)] == ["trash"]
    monkeypatch.undo()
    report = journal.recover(root)
    assert len(report.completed) == 1 and not report.kept
    purge_by(how, root, entry_id)
    gone_for_good(root, directory, file)


def test_purge_after_a_resumed_restore_is_refused_and_deletes_nothing(managed, monkeypatch):
    """A half-done Restore of the entry is finished forward; the meeting is back, not purged."""
    root, sid, directory, file = managed
    original = file.read_bytes()
    (meeting,) = rows(root)
    _root, entry_id = trash_meeting(meeting)
    real_rename = storage._rename

    def rename(source, target):
        if Path(source).name == "meeting.opus":
            raise OSError(errno.EIO, "generated published-file restore failure")
        real_rename(source, target)

    monkeypatch.setattr(storage, "_rename", rename)
    with pytest.raises(storage.Failed):
        storage.restore(root, entry_id)
    monkeypatch.undo()
    with pytest.raises(storage.Refused, match="no such entry"):
        storage.purge(root, entry_id)
    assert not pending(root) and file.read_bytes() == original and directory.is_dir()
