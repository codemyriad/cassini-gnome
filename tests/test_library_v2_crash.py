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

from cassini_viewer import portable  # noqa: E402
from cassini_viewer.library_v2 import edit, finalize, journal  # noqa: E402

SRC = str(Path(__file__).parents[1] / "src")
CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from cassini_viewer.library_v2 import edit, finalize, journal, publish
    operation, target, root, sid = sys.argv[2:6]

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
    elif operation == "delete":
        layout = journal.Layout(Path(root))
        with journal.LibraryLock(layout):
            name = publish.find_file(layout, "Recording", sid)
            publish.delete(layout, origin="Recording", session_id=sid, file_name=name)
    """
)
OLD = "2026-10-30--18.02-Weekly sync.opus"
NEW = "2026-10-30--18.02-Renamed.opus"


def crash(operation, step, root, sid):
    result = subprocess.run([sys.executable, "-c", CHILD, SRC, operation, step, str(root), sid], timeout=120)
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


PUBLISH = ["journaled", "mixed", "stage-written", "staged", "before-rename", "renamed", "after-fsync", "after-meeting"]


@pytest.mark.parametrize("step", PUBLISH)
def test_crash_while_publishing(tmp_path, step):
    sid, directory = simple_session(tmp_path, 0.5)
    sources = {p.name: p.read_bytes() for p in directory.glob("track-*.wav")}
    crash("publish", step, tmp_path, sid)
    found = recovered(tmp_path)
    assert list(found) == [("Recording", sid)] and len(found[("Recording", sid)]) == 1
    assert {p.name: p.read_bytes() for p in directory.glob("track-*.wav")} == sources
    if PUBLISH.index(step) < PUBLISH.index("staged"):
        # Before the stage was known complete nothing was published; a retry publishes.
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


@pytest.mark.parametrize("step", ["journaled", "file-removed", "session-removed"])
def test_crash_while_deleting_never_resurrects_the_session(published, tmp_path, step):
    root, sid, directory = library(published, tmp_path)
    crash("delete", step, root, sid)
    assert recovered(root) == {} and root_files(root) == [] and not directory.exists()
