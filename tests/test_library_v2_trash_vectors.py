# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §7.1 conformance: every case and every crash point of the vendored cassini-ux
`trash-vectors.json`, replayed against library_v2.trash on a real filesystem.

The vectors' meeting files hold the reference model's text identities (`opus id=<folder>`,
`opus sha=<hex>`); trash.IDENTITY is pointed at that text, everything else (lock, journal,
no-replace renames, fsyncs, startup recovery) is the shipping code. A crash after step k is
an exception raised by trash.CHECKPOINT after the k-th applied step (k counts the journal
write and its retirement, as in the vectors), which no code path catches, so nothing after
that step runs. Recovery is then the real locked replay.
"""

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from cassini_talk.library_v2 import journal, trash

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / "tests/ux/library-v2/trash-vectors.json").read_text(encoding="utf-8"))
CASES = VECTORS["cases"]
UNTITLED = VECTORS["untitled"]
# Created by LibraryLock (Layout.ensure) on every locked operation; the model has no lock.
INFRASTRUCTURE = {".cassini", ".cassini/sessions", ".cassini/journal", ".cassini/staging"}


class Crash(BaseException):
    """SIGKILL stand-in: a BaseException, so no `except Exception/OSError` handler runs."""


def model_identity(path, *, need_sha=True):
    kind, _, value = Path(path).read_text(encoding="utf-8").partition("=")
    if kind == "opus id":
        return {"session": value}
    if kind == "opus sha":
        return {"sha": value} if need_sha else None
    raise ValueError("not a model meeting file")


@pytest.fixture(autouse=True)
def identities(monkeypatch):
    monkeypatch.setattr(trash, "IDENTITY", model_identity)


def build(root, files, dirs=()):
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for name in dirs:
        (root / name).mkdir(parents=True, exist_ok=True)


def snapshot(root):
    files, dirs = {}, []
    for directory, subdirs, names in os.walk(root):
        relative = Path(directory).relative_to(root).as_posix()
        relative = "" if relative == "." else relative
        for name in names:
            key = f"{relative}/{name}" if relative else name
            if key == ".cassini/library.lock":
                continue
            files[key] = (Path(directory) / name).read_text(encoding="utf-8")
        if relative and not names and not subdirs:
            dirs.append(relative)
    empty = sorted(d for d in dirs if d not in INFRASTRUCTURE)
    return {"files": dict(sorted(files.items())), "emptyDirs": empty}


def library_rows(root):
    """The reference model's Library rows (S-12 §1, LIBRARY-RULES R1) over the real folder."""
    rows, published = [], set()
    for name in sorted(os.listdir(root)):
        path = root / name
        if name.startswith("."):
            continue
        if path.is_file() and name.endswith(".opus"):
            key = trash.identity_key(model_identity(path))
            published.add(key)
            rows.append({"kind": "published", "name": name, "identity": key})
        elif path.is_dir() and trash.SESSION_NAME.match(name):
            rows.append({"kind": "v1Folder", "name": name, "identity": name})
    sessions = root / trash.SESSIONS
    for name in sorted(os.listdir(sessions)) if sessions.is_dir() else ():
        if name not in published and trash.SESSION_NAME.match(name):
            rows.append({"kind": "unpublished", "name": name, "identity": name})
    return rows


def view(root):
    return {
        "state": snapshot(root),
        "libraryRows": library_rows(root),
        "trashRows": [
            {"entryId": e.entry_id, "title": e.title, "deletedAtUtc": e.deleted_at_utc, "identity": e.key}
            for e in trash.entries(root)
        ],
    }


def normalized(expected_view):
    state = expected_view["state"]
    return dict(
        expected_view,
        state={"files": state["files"], "emptyDirs": [d for d in state["emptyDirs"] if d not in INFRASTRUCTURE]},
    )


def perform(root, request):
    op = request["op"]
    if op == "trash":
        return trash.move_to_trash(
            root,
            published=request["published"],
            session=request["session"],
            title=request["title"],
            recorded_at_utc=request["recordedAtUtc"],
            duration_ms=request["durationMs"],
            name_prefix=request.get("namePrefix"),
            active_lease=bool(request.get("activeLease")),
            now=trash.parse_utc(request["deletedAtUtc"]),
            entry_id=request["entryId"],
        )
    if op == "restore":
        return trash.restore(root, request["entryId"], untitled=UNTITLED)
    if op == "purge":
        return trash.purge(root, request["entryId"])
    if op == "emptyTrash":
        return trash.empty_trash(root)
    raise AssertionError(op)


def start(root, case):
    """input.files (+ dirs), input.before applied fully, then input.putAfterBefore."""
    build(root, case["input"]["files"], case["input"].get("dirs", ()))
    for request in case["input"].get("before", []):
        perform(root, request)
    build(root, case["input"].get("putAfterBefore", {}))


@contextmanager
def journals_recorded(monkeypatch):
    seen = []
    original = trash._new_journal

    def record(head, steps):
        entry = original(head, steps)
        seen.append(entry)
        return entry

    monkeypatch.setattr(trash, "_new_journal", record)
    yield seen
    monkeypatch.setattr(trash, "_new_journal", original)


def expected_journals(expected):
    return [json.loads(s["content"]) for s in expected["steps"] if s["do"] == "writeJournal"]


def recover(root):
    """Startup recovery: the Library lock, then the trash replay and sweep (S-12 §7.1)."""
    layout = journal.Layout(root)
    with journal.LibraryLock(layout):
        _done, _kept, messages = trash.recover(layout, untitled=UNTITLED)
    return messages


def report(messages):
    """Model report lines name the entry; ours too (op ids differ, entry ids are the vectors')."""
    return [m for m in messages if not m.startswith("rebuilt ")]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case, tmp_path, monkeypatch):
    expected = case["expected"]
    root = tmp_path / "Library"
    start(root, case)
    assert view(root) == normalized(expected["before"])
    if expected["result"] == "refused":
        with journals_recorded(monkeypatch) as seen, pytest.raises(trash.Refused):
            perform(root, case["input"]["request"])
        assert not seen, "a refused operation wrote a journal entry"
        assert view(root) == normalized(expected["after"])
        assert not journal.pending(journal.Layout(root), cleanup=False).entries
        return
    with journals_recorded(monkeypatch) as seen:
        perform(root, case["input"]["request"])
    model = expected_journals(expected)
    assert [(j["kind"], j["entryId"], j["steps"]) for j in seen] == [
        (j["kind"], j["entryId"], j["steps"]) for j in model
    ]
    assert view(root) == normalized(expected["after"])
    assert not os.listdir(root / ".cassini/journal")


CRASH_POINTS = [
    pytest.param(case, point, id=f"{case['id']}@{point['crashAfterStep']}")
    for case in CASES
    if case["expected"]["result"] == "ok"
    for point in case["expected"]["crashMatrix"]
]


def test_every_crash_point_is_covered():
    assert len(CASES) == 30
    assert len(CRASH_POINTS) == 125
    assert sum(len(c["expected"].get("crashMatrix", [])) for c in CASES) == 125


@pytest.mark.parametrize(("case", "point"), CRASH_POINTS)
def test_crash_point(case, point, tmp_path, monkeypatch):
    k = point["crashAfterStep"]
    root = tmp_path / "Library"
    start(root, case)
    applied = []

    def checkpoint(_count):
        applied.append(_count)
        if len(applied) == k:
            raise Crash(k)

    if k:
        monkeypatch.setattr(trash, "CHECKPOINT", checkpoint)
        with pytest.raises(Crash):
            perform(root, case["input"]["request"])
        monkeypatch.setattr(trash, "CHECKPOINT", None)
        assert len(applied) == k
    messages = recover(root)
    assert report(messages) == point["recovery"]
    assert view(root) == normalized(point["afterRecovery"])
    assert not journal.pending(journal.Layout(root), cleanup=False).entries


@pytest.mark.parametrize("vector", VECTORS["daysLeft"], ids=[v["id"] for v in VECTORS["daysLeft"]])
def test_days_left(vector):
    n, expired = trash.days_left(vector["deletedAtUtc"], vector["nowUtc"])
    assert (n, expired) == (vector["daysLeft"], vector["expired"])
    assert trash.display_key(vector["deletedAtUtc"], vector["nowUtc"]) == vector["display"]


def test_startup_recovery_through_the_library_journal(tmp_path, monkeypatch):
    """The app's startup path (journal.recover -> replay) finishes a half-done move too."""
    case = next(c for c in CASES if c["id"] == "b-trash")
    root = tmp_path / "Library"
    start(root, case)
    calls = []

    def checkpoint(_count):
        calls.append(_count)
        if len(calls) == 5:  # journal, 2 mkdir, entry.json, the published file moved
            raise Crash()

    monkeypatch.setattr(trash, "CHECKPOINT", checkpoint)
    with pytest.raises(Crash):
        perform(root, case["input"]["request"])
    monkeypatch.setattr(trash, "CHECKPOINT", None)
    assert (root / ".cassini/sessions").iterdir()  # the working folder is still live
    report = journal.recover(root)
    assert len(report.completed) == 1 and not report.kept
    assert view(root) == normalized(case["expected"]["after"])


def test_conflict_keeps_both_copies_and_the_journal(tmp_path, monkeypatch):
    case = next(c for c in CASES if c["id"] == "b-trash")
    root = tmp_path / "Library"
    start(root, case)
    calls = []

    def checkpoint(_count):
        calls.append(_count)
        if len(calls) == 4:  # entry.json written, nothing moved yet
            raise Crash()

    monkeypatch.setattr(trash, "CHECKPOINT", checkpoint)
    with pytest.raises(Crash):
        perform(root, case["input"]["request"])
    monkeypatch.setattr(trash, "CHECKPOINT", None)
    entry = root / ".cassini/trash" / case["input"]["request"]["entryId"]
    (entry / "meeting.opus").write_text("opus id=someone-else-put-this-here", encoding="utf-8")
    messages = recover(root)
    assert len(messages) == 1 and messages[0].startswith("conflict in trash")
    assert (root / case["input"]["request"]["published"]).exists()
    assert (entry / "meeting.opus").exists()
    assert journal.pending(journal.Layout(root), cleanup=False).entries  # kept, never deleted
    report = journal.recover(root)
    assert report.kept and not report.completed


def test_damaged_journal_cannot_remove_outside_its_entry(tmp_path):
    root = tmp_path / "Library"
    build(root, {"keep.opus": "opus id=Recording-0b6f3c2e-1111-4a5b-8c9d-000000000001"})
    layout = journal.Layout(root)
    layout.ensure()
    op = "5d0c3a7e-0000-4000-8000-0000000000f1"
    bad = {
        "op": op,
        "kind": "purge",
        "createdAt": "2026-11-14T09:30:00.000000Z",
        "entryId": "5d0c3a7e-0000-4000-8000-0000000000e1",
        "identity": None,
        "steps": [{"do": "removeTree", "path": "keep.opus"}],
    }
    journal.write(layout, bad)
    found = journal.pending(layout, cleanup=False)
    assert found.damaged == [f"{op}.json"] and not found.entries
    journal.recover(root)
    assert (root / "keep.opus").exists()


def test_restore_name_retries_when_the_filesystem_rejects_it(tmp_path, monkeypatch):
    """EEXIST from the no-replace rename (a name the casefold key did not see): next suffix."""
    case = next(c for c in CASES if c["id"] == "a-restore")
    root = tmp_path / "Library"
    start(root, case)
    original = trash._rename
    rejected = []

    def rename(source, target):
        if Path(target).name == "2026-10-30--18.02-Weekly sync.opus" and not rejected:
            rejected.append(target)
            raise FileExistsError(17, "File exists", str(target))
        return original(source, target)

    monkeypatch.setattr(trash, "_rename", rename)
    name = trash.restore(root, case["input"]["request"]["entryId"], untitled=UNTITLED)
    assert name == "2026-10-30--18.02-Weekly sync (2).opus"
    assert (root / name).read_text() == "opus id=Recording-0b6f3c2e-1111-4a5b-8c9d-000000000001"
    assert not os.listdir(root / ".cassini/journal")


def test_failed_first_move_changes_nothing(tmp_path, monkeypatch):
    """A purge whose first rename fails leaves the entry listed and no journal (trash.deleteFailed)."""
    case = next(c for c in CASES if c["id"] == "a-purge")
    root = tmp_path / "Library"
    start(root, case)
    before = view(root)

    def broken(_source, _target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(trash, "_rename", broken)
    with pytest.raises(PermissionError):
        trash.purge(root, case["input"]["request"]["entryId"])
    assert view(root) == before
    assert not os.listdir(root / ".cassini/journal")


def test_failed_trash_move_rolls_back_the_new_entry(tmp_path, monkeypatch):
    case = next(c for c in CASES if c["id"] == "a-trash-published-only")
    root = tmp_path / "Library"
    start(root, case)
    before = view(root)

    def broken(_source, _target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(trash, "_rename", broken)
    with pytest.raises(PermissionError):
        perform(root, case["input"]["request"])
    after = view(root)
    assert after["libraryRows"] == before["libraryRows"] and not after["trashRows"]
    assert not [p for p in after["state"]["files"] if p.startswith(".cassini/")]


def test_missing_entry_json_is_rebuilt(tmp_path):
    case = next(c for c in CASES if c["id"] == "c-restore-unpublished")
    root = tmp_path / "Library"
    start(root, case)
    entry_id = case["input"]["request"]["entryId"]
    (root / ".cassini/trash" / entry_id / "entry.json").unlink()
    assert not trash.entries(root)
    messages = recover(root)
    assert messages == [f"rebuilt {entry_id}"]
    rows = trash.entries(root)
    assert [r.entry_id for r in rows] == [entry_id]
    assert rows[0].key == "Recording-0b6f3c2e-3333-4a5b-8c9d-000000000003"
    trash.restore(root, entry_id, untitled=UNTITLED)
    assert (root / ".cassini/sessions/Recording-0b6f3c2e-3333-4a5b-8c9d-000000000003/meeting.json").exists()


def test_expiry_purges_only_entries_past_thirty_days(tmp_path):
    case = next(c for c in CASES if c["id"] == "empty-trash")
    root = tmp_path / "Library"
    start(root, case)
    # Deleted at 09:30 and 09:45 on 2026-11-14: at 09:40 thirty days later only the first is due.
    purged = trash.expire(root, trash.parse_utc("2026-12-14T09:40:00Z"))
    assert [trash.identity_key(i) for i in purged] == ["Recording-0b6f3c2e-1111-4a5b-8c9d-000000000001"]
    assert [r.title for r in trash.entries(root)] == ["Interrupted meeting"]
    assert trash.expire(root, trash.parse_utc("2026-12-14T09:44:59Z")) == []
    assert len(trash.expire(root, trash.parse_utc("2026-12-14T09:45:00Z"))) == 1
    assert not trash.entries(root)
    assert not [p for p in snapshot(root)["files"] if p.startswith(".cassini/")]


def test_infrastructure_copy_for_crash_isolation(tmp_path):
    """Sanity: the crash tests start from exactly the state the vectors start from."""
    case = next(c for c in CASES if c["id"] == "b-restore")
    a, b = tmp_path / "a", tmp_path / "b"
    start(a, case)
    shutil.copytree(a, b, symlinks=True)
    assert view(a) == view(b) == normalized(case["expected"]["before"])
