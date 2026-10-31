# SPDX-License-Identifier: GPL-3.0-or-later
"""§9.3 crash matrix: SIGKILL a real process at every §4.6 and §6/§7 step, then recover.

After startup recovery there is exactly one meeting row with the old or the new state
intact, no stage is listed, the journal is empty, and a deleted session never comes back.
"""

import os
import shutil
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")

from library_v2_support import meeting_json, root_files, rows, simple_session  # noqa: E402

from cassini_talk import portable  # noqa: E402
from cassini_talk.library_v2 import edit, finalize, journal  # noqa: E402

SRC = str(Path(__file__).parents[1] / "src")
CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from cassini_talk.library_v2 import edit, finalize, journal, publish
    operation, target, root, sid = sys.argv[2:6]
    if os.environ.get("CASSINI_TEST_CASEFOLD"):
        sys.path.insert(0, os.environ["CASSINI_TEST_DIR"])
        from library_v2_support import emulate_case_insensitive
        emulate_case_insensitive(setattr)

    def checkpoint(step):
        if step == target:
            os.kill(os.getpid(), signal.SIGKILL)

    publish.CHECKPOINT = checkpoint
    if operation == "publish":
        finalize.finalize(root, sid, zone="Europe/Rome")
    elif operation == "replace":
        edit.edit_meeting(root, "Recording", sid, {"notes": [{"atMs": 1, "text": "after"}]})
    elif operation == "rename":
        edit.edit_meeting(root, "Recording", sid, {"title": "Renamed", "titleSource": "user"})
    elif operation == "case":
        edit.edit_meeting(root, "Recording", sid, {"title": "WEEKLY sync", "titleSource": "user"})
    elif operation == "delete":
        layout = journal.Layout(Path(root))
        with journal.LibraryLock(layout):
            name = publish.find_file(layout, "Recording", sid)
            publish.delete(layout, origin="Recording", session_id=sid, file_name=name)
    """
)
OLD = "2026-10-30--18.02-Weekly sync.opus"
NEW = "2026-10-30--18.02-Renamed.opus"


RECOVER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from cassini_talk.library_v2 import journal
    report = journal.recover(sys.argv[2])
    assert not report.kept and not report.orphans, report
    """
)


def crash(operation, step, root, sid, env=None):
    result = subprocess.run([sys.executable, "-c", CHILD, SRC, operation, step, str(root), sid], timeout=120, env=env)
    assert result.returncode == -signal.SIGKILL, f"{step} was never reached"


def recovered(root):
    report = journal.recover(root)
    assert not report.kept and not report.orphans, report
    layout = journal.Layout(Path(root))
    assert os.listdir(layout.journal) == [] and os.listdir(layout.staging) == []
    assert not [n for n in os.listdir(root) if n.endswith((".stage", ".partial"))]
    return rows(root)


def state(root, name):
    path = Path(root) / name
    edit.verify(path)
    value = portable.read_meeting(str(path)).manifest
    return value["x"]["codemyriad.io"]["revision"], value["x"].get("gocassini.com", {}).get("notes")


# The first three run without the Library lock, before anything is journaled (§11 step-3a Q10).
PUBLISH = [
    "building",
    "mixed",
    "stage-written",
    "journaled",
    "before-rename",
    "renamed",
    "after-fsync",
    "after-meeting",
]


@pytest.mark.parametrize("step", PUBLISH)
def test_crash_while_publishing(tmp_path, step):
    sid, directory = simple_session(tmp_path, 0.5)
    sources = {p.name: p.read_bytes() for p in directory.glob("track-*.wav")}
    crash("publish", step, tmp_path, sid)
    found = recovered(tmp_path)
    assert list(found) == [("Recording", sid)] and len(found[("Recording", sid)]) == 1
    assert {p.name: p.read_bytes() for p in directory.glob("track-*.wav")} == sources
    if PUBLISH.index(step) < PUBLISH.index("journaled"):
        # An unjournaled build of a dead process: recovery removed it; a retry publishes (Q6).
        assert found[("Recording", sid)] == [("session", directory.name, 0)] and meeting_json(directory) is None
        assert finalize.finalize(tmp_path, sid, zone="Europe/Rome").name == OLD
    else:
        assert found[("Recording", sid)] == [("file", OLD, 1)] and state(tmp_path, OLD) == (1, None)
        assert meeting_json(directory)["published"] is True and meeting_json(directory)["revision"] == 1
    assert root_files(tmp_path) == [OLD]


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    root = tmp_path_factory.mktemp("published")
    sid, _directory = simple_session(root, 0.5)
    assert finalize.finalize(root, sid, zone="Europe/Rome").name == OLD
    return root, sid


def library(published, tmp_path):
    root, sid = published
    copy = tmp_path / "library"
    shutil.copytree(root, copy, symlinks=True)
    return copy, sid, journal.Layout(copy).session("Recording", sid)


REPLACE = ["journaled", "stage-written", "staged", "replaced", "after-fsync", "after-meeting"]


@pytest.mark.parametrize("step", REPLACE)
def test_crash_while_replacing_in_place(published, tmp_path, step):
    root, sid, directory = library(published, tmp_path)
    crash("replace", step, root, sid)
    found = recovered(root)
    applied = REPLACE.index(step) >= REPLACE.index("staged")
    assert found == {("Recording", sid): [("file", OLD, 2 if applied else 1)]}
    assert state(root, OLD) == ((2, [dict(atMs=1, text="after")]) if applied else (1, None))
    assert meeting_json(directory)["revision"] == (2 if applied else 1)


RENAME = ["journaled", "stage-written", "staged", "before-rename", "renamed", "after-fsync", "after-meeting",
          "after-remove-old"]  # fmt: skip


@pytest.mark.parametrize("step", RENAME)
def test_crash_while_renaming(published, tmp_path, step):
    root, sid, directory = library(published, tmp_path)
    crash("rename", step, root, sid)
    found = recovered(root)
    applied = RENAME.index(step) >= RENAME.index("staged")
    name = NEW if applied else OLD
    assert found == {("Recording", sid): [("file", name, 2 if applied else 1)]} and root_files(root) == [name]
    assert state(root, name)[0] == (2 if applied else 1)
    assert portable.read_meeting(str(root / name)).tags["TITLE"] == ("Renamed" if applied else "Weekly sync")
    assert meeting_json(directory)["revision"] == (2 if applied else 1)


@pytest.mark.parametrize("step", ["journaled", "before-rename", "renamed", "after-meeting"])
def test_crash_during_a_case_only_rename_keeps_the_base_name(published, tmp_path, step):
    root, sid, directory = library(published, tmp_path)
    child = CHILD.replace(
        '{"title": "Renamed", "titleSource": "user"}', '{"title": "weekly SYNC", "titleSource": "user"}'
    )
    result = subprocess.run([sys.executable, "-c", child, SRC, "rename", step, str(root), sid], timeout=120)
    assert result.returncode == -signal.SIGKILL
    found = recovered(root)
    applied = step != "journaled"
    name = "2026-10-30--18.02-weekly SYNC.opus" if applied else OLD
    assert found == {("Recording", sid): [("file", name, 2 if applied else 1)]} and root_files(root) == [name]


@pytest.mark.parametrize("step", ["journaled", "file-trashed", "session-trashed"])
def test_crash_while_trashing_never_resurrects_the_session(published, tmp_path, step):
    """§11 step-3a Q9: the file and the session folder both go to the (isolated) Trash, intact."""
    root, sid, directory = library(published, tmp_path)
    original = (root / OLD).read_bytes()
    session_files = {p.name: p.read_bytes() for p in directory.iterdir()}
    env = dict(os.environ, XDG_DATA_HOME=str(tmp_path / "xdg"), HOME=str(tmp_path / "home"))
    (tmp_path / "xdg").mkdir()
    (tmp_path / "home").mkdir()
    crash("delete", step, root, sid, env=env)
    subprocess.run([sys.executable, "-c", RECOVER, SRC, str(root)], timeout=120, env=env, check=True)
    assert rows(root) == {} and root_files(root) == [] and not directory.exists()
    layout = journal.Layout(root)
    assert os.listdir(layout.journal) == [] and os.listdir(layout.staging) == []
    trash = tmp_path / "xdg" / "Trash" / "files"
    assert (trash / OLD).read_bytes() == original
    assert {p.name: p.read_bytes() for p in (trash / directory.name).iterdir()} == session_files


CASE = ["journaled", "before-rename", "before-case-temp", "case-moved", "case-temp", "renamed", "after-meeting",
        "after-remove-old"]  # fmt: skip


@pytest.mark.parametrize("step", CASE)
def test_crash_during_a_case_only_rename_on_a_case_insensitive_filesystem(published, tmp_path, monkeypatch, step):
    """§11 step-3a Q7 under casefold emulation: the original is never left hidden at its temporary name."""
    from library_v2_support import emulate_case_insensitive

    root, sid, directory = library(published, tmp_path)
    env = dict(os.environ, CASSINI_TEST_CASEFOLD="1", CASSINI_TEST_DIR=str(Path(__file__).parent))
    crash("case", step, root, sid, env=env)
    emulate_case_insensitive(monkeypatch.setattr)
    found = recovered(root)
    applied = step != "journaled"
    name = "2026-10-30--18.02-WEEKLY sync.opus" if applied else OLD
    assert found == {("Recording", sid): [("file", name, 2 if applied else 1)]} and root_files(root) == [name]
    assert not [n for n in os.listdir(root) if n.startswith(".cassini-case-")]
    assert state(root, name)[0] == (2 if applied else 1)
    assert meeting_json(directory)["revision"] == (2 if applied else 1)


def test_resume_never_removes_the_old_name_when_the_new_name_holds_another_file(published, tmp_path):
    """Review item 4: a rename that crashed after placing its file, whose new name was then taken."""
    root, sid, directory = library(published, tmp_path)
    other_id, _other = simple_session(root, 0.5, title="Other")
    other = finalize.finalize(root, other_id, zone="Europe/Rome").name
    crash("rename", "renamed", root, sid)
    (root / NEW).write_bytes((root / other).read_bytes())  # another meeting's file now at the new name
    report = journal.recover(root)
    assert len(report.kept) == 1 and (root / OLD).exists()  # the meeting's own copy is never removed
    assert state(root, OLD)[0] == 1
