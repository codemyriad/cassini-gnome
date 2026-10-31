# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct regression tests for #130's commit guards: each fails when its guard is removed.

The broader review and crash tests reach these guards only behind earlier checks, so each
test here builds the one state where the guard alone decides what happens.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")

from library_v2_support import emulate_case_insensitive, meeting_json, root_files, simple_session  # noqa: E402

from cassini_talk.library_v2 import edit, journal, publish  # noqa: E402
from cassini_talk.library_v2 import finalize as fin  # noqa: E402

OLD = "2026-10-30--18.02-Weekly sync.opus"
NEW = "2026-10-30--18.02-Renamed.opus"


class Died(BaseException):
    """A process death between two steps, without leaving the test process."""


@pytest.fixture(autouse=True)
def no_checkpoint(monkeypatch):
    monkeypatch.setattr(publish, "CHECKPOINT", None)


def published(root, title="Weekly sync"):
    sid, directory = simple_session(root, 0.5, title=title)
    name = fin.finalize(root, sid, zone="Europe/Rome").name
    return sid, directory, name


def pending(root):
    return [json.loads(p.read_text()) for p in sorted((Path(root) / ".cassini/journal").glob("*.json"))]


def test_settle_refuses_to_remove_the_old_name_when_the_destination_changed(tmp_path, monkeypatch):
    """_settle's own destination check: the new name was swapped for another meeting's file right
    after the rename, so the old name is the meeting's only copy and must stay."""
    sid, _directory, _name = published(tmp_path)
    _other_id, _other_dir, other = published(tmp_path, "Other")
    other_bytes = (tmp_path / other).read_bytes()

    def swap(step):
        if step == "renamed":
            (tmp_path / "swap.tmp").write_bytes(other_bytes)
            os.replace(tmp_path / "swap.tmp", tmp_path / NEW)  # a new inode: the stage is gone either way

    monkeypatch.setattr(publish, "CHECKPOINT", swap)
    with pytest.raises(publish.PendingCommit):
        edit.edit_meeting(tmp_path, "Recording", sid, dict(title="Renamed", titleSource="user"))
    assert publish.file_identity(tmp_path / OLD)[1:] == (sid, 1)
    assert len(pending(tmp_path)) == 1


def test_case_alias_refuses_a_foreign_temporary_and_settle_keeps_it(tmp_path, monkeypatch):
    """A case-only rename journaled its temporary name and died before moving the original;
    another file then appeared at that temporary name. The rename must not treat the name as
    its own alias (it takes the next suffix), and the foreign file must survive the commit."""
    emulate_case_insensitive(monkeypatch.setattr)
    sid, directory, name = published(tmp_path)

    def die(step):
        if step == "before-case-temp":
            raise Died()

    monkeypatch.setattr(publish, "CHECKPOINT", die)
    with pytest.raises(publish.PendingCommit):
        edit.edit_meeting(tmp_path, "Recording", sid, dict(title="WEEKLY sync", titleSource="user"))
    monkeypatch.setattr(publish, "CHECKPOINT", None)
    (entry,) = pending(tmp_path)
    foreign = tmp_path / entry["caseTemp"]
    assert not foreign.exists() and (tmp_path / name).exists()
    foreign.write_bytes(b"not this meeting")
    report = journal.recover(tmp_path)
    assert not report.kept and pending(tmp_path) == []
    assert root_files(tmp_path) == ["2026-10-30--18.02-WEEKLY sync (2).opus"]
    assert foreign.read_bytes() == b"not this meeting"
    assert meeting_json(directory)["revision"] == 2


def symlinked_session(root, directory, *, keep_meeting=True):
    elsewhere = Path(root) / "elsewhere"
    os.rename(directory, elsewhere)
    if not keep_meeting:
        (elsewhere / "meeting.json").unlink(missing_ok=True)
    os.symlink(elsewhere, directory)
    return elsewhere


def test_commit_edit_refuses_a_symlinked_session_folder(tmp_path):
    sid, directory, name = published(tmp_path)
    elsewhere = symlinked_session(tmp_path, directory)
    layout = journal.Layout(tmp_path)

    def write_stage(stage):
        shutil.copyfile(tmp_path / name, stage)

    with journal.LibraryLock(layout), pytest.raises(publish.Kept):
        publish.commit_edit(
            layout,
            origin="Recording",
            session_id=sid,
            old_name=name,
            base_revision=1,
            file_revision=1,
            new_revision=2,
            name=None,
            meeting_update=dict(title="Through a link", titleSource="user"),
            write_stage=write_stage,
        )
    assert pending(tmp_path) == [] and os.listdir(layout.staging) == []
    assert json.loads((elsewhere / "meeting.json").read_text())["revision"] == 1


def test_meeting_step_refuses_a_session_folder_that_became_a_link(tmp_path, monkeypatch):
    """The entry was journaled for a real folder (meeting.json missing, so it would be rebuilt);
    before recovery the folder was replaced by a link. Nothing may be written through it."""
    sid, directory, name = published(tmp_path)
    (directory / "meeting.json").unlink()

    def die(step):
        if step == "replaced":
            raise Died()

    monkeypatch.setattr(publish, "CHECKPOINT", die)
    with pytest.raises(Died):
        edit.edit_meeting(tmp_path, "Recording", sid, dict(notes=[dict(atMs=1, text="n")]))
    monkeypatch.setattr(publish, "CHECKPOINT", None)
    (entry,) = pending(tmp_path)
    assert entry["meetingRebuilt"] is True
    elsewhere = symlinked_session(tmp_path, directory, keep_meeting=False)
    report = journal.recover(tmp_path)
    assert len(report.kept) == 1 and "link" in report.kept[0][1]
    assert not (elsewhere / "meeting.json").exists()
