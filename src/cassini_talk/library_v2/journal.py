# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §7: the Library layout, the Library lock and the durable operation journal.

Every publish, replace, rename and delete holds `.cassini/library.lock` (flock) and
first writes `.cassini/journal/<op>.json` durably (file and directory fsynced). An
entry records its kind, the meeting identity, old and new paths, the expected base
revision, the new revision and its stage. Paths are stored relative to the Library
root, so a moved Library still replays. replay() resumes every entry idempotently
(publish.resume); an entry that cannot finish safely is kept and reported, with
its stage, and is never resolved by deleting anything.

Entry phases: "prepared" (journaled, the stage is not yet known to be complete),
"staged" (the stage is complete, fsynced and validated), "conflict" (a stage that
no longer fits what is live: kept for the user, never applied or deleted), and
"intent" for deletions.
"""

import contextlib
import errno
import fcntl
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from ..library_files import basename
from ..recording_session import strict_json
from . import fsutil

META = ".cassini"
SESSION_NAME = re.compile(r"(Recording|Received)-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z", re.I)
KINDS = ("publish", "replace", "rename", "delete")
MAX_ENTRY = 1 << 20


class LockBusy(RuntimeError):
    """Another process holds the Library lock."""


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def meta(self):
        return self.root / META

    @property
    def sessions(self):
        return self.meta / "sessions"

    @property
    def journal(self):
        return self.meta / "journal"

    @property
    def staging(self):
        return self.meta / "staging"

    @property
    def lock(self):
        return self.meta / "library.lock"

    def ensure(self):
        """Create .cassini/{sessions,journal,staging}, each new directory made durable in its parent."""
        for directory in (self.meta, self.sessions, self.journal, self.staging):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                if directory.is_symlink() or not directory.is_dir():
                    raise NotADirectoryError(
                        errno.ENOTDIR, "Library folder is not a directory", str(directory)
                    ) from None
                continue
            fsutil.fsync_dir(directory.parent)
        if os.stat(self.staging).st_dev != os.stat(self.root).st_dev:
            raise OSError(errno.EXDEV, "The staging folder must be on the Library's filesystem", str(self.staging))

    def relative(self, path):
        """A root-relative POSIX path for the journal; ValueError outside the root."""
        return PurePosixPath(Path(path).relative_to(self.root)).as_posix()

    def resolve(self, relative):
        """The absolute path of a journal path; ValueError for anything not confined to the root."""
        if not isinstance(relative, str) or not relative:
            raise ValueError("unsafe-journal-path")
        parts = PurePosixPath(relative).parts
        if relative.startswith("/") or any(part in ("", ".", "..") for part in parts) or "\\" in relative:
            raise ValueError("unsafe-journal-path")
        return self.root.joinpath(*parts)

    def session(self, origin, session_id):
        return self.sessions / f"{origin}-{session_id}"


def identity_of(session_dir):
    """(origin, sessionId) from a session folder name, or None."""
    match = SESSION_NAME.match(Path(session_dir).name)
    return (match.group(1), match.group(2)) if match else None


class LibraryLock:
    """flock(LOCK_EX) on .cassini/library.lock for the duration of a `with` block.

    flock locks belong to the open file description, so two LibraryLock objects
    exclude each other even inside one process.
    """

    def __init__(self, layout, *, blocking=True):
        self.layout = layout
        self.blocking = blocking
        self.fd = None

    def __enter__(self):
        self.layout.ensure()
        fd = os.open(self.layout.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            raise LockBusy("Another Cassini window is changing this Library") from None
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_exc):
        fd, self.fd = self.fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def new_entry(kind, *, origin, session_id, op=None, **members):
    if kind not in KINDS:
        raise ValueError("unknown journal kind")
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return dict(
        op=op or str(uuid4()), kind=kind, createdAt=now, identity=dict(origin=origin, sessionId=session_id), **members
    )


def _path(layout, op):
    if str(UUID(op)) != op:
        raise ValueError("invalid-journal-op")
    return layout.journal / f"{op}.json"


def write(layout, entry):
    """Durably (re)write one entry: the file and the journal directory are fsynced."""
    data = json.dumps(entry, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    fsutil.write_durable(_path(layout, entry["op"]), data)


def retire(layout, op):
    try:
        _path(layout, op).unlink()
    except FileNotFoundError:
        return
    fsutil.fsync_dir(layout.journal)


@dataclass
class Pending:
    entries: list = field(default_factory=list)
    damaged: list = field(default_factory=list)  # file names that are not valid entries; kept


def pending(layout, *, cleanup=True):
    """Committed entries, oldest first. A leftover `.json.partial` was never committed, so no copy
    was touched after it. Locked recovery removes it with cleanup=True. Unlocked
    readers must use cleanup=False: a partial may belong to a live writer.
    Unreadable entries are kept and reported.
    """
    result = Pending()
    try:
        names = sorted(os.listdir(layout.journal))
    except FileNotFoundError:
        return result
    for name in names:
        path = layout.journal / name
        if name.endswith(".json.partial"):
            if cleanup:
                path.unlink(missing_ok=True)
            continue
        if not name.endswith(".json"):
            continue
        try:
            with open(path, "rb") as stream:
                raw = stream.read(MAX_ENTRY + 1)
            entry = strict_json(raw) if len(raw) <= MAX_ENTRY else None
            if not isinstance(entry, dict) or name != f"{entry.get('op')}.json" or _path(layout, entry["op"]) != path:
                raise ValueError("invalid-journal-entry")
            check_entry(layout, entry)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            result.damaged.append(name)
            continue
        result.entries.append(entry)
    result.entries.sort(key=lambda e: (str(e.get("createdAt")), e["op"]))
    return result


def _basename(value, *, optional=True):
    if value is None and optional:
        return
    if not isinstance(value, str) or basename(value) != value or "/" in value:
        raise ValueError("journal path is not one name in the Library root")


def _revision(value, *, optional=False):
    if value is None and optional:
        return
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError("invalid journal revision")


def check_entry(layout, entry):
    """Structure, types and confined paths of one entry, per kind; ValueError when malformed."""
    if entry.get("kind") not in KINDS or not isinstance(entry.get("createdAt"), str):
        raise ValueError("invalid journal kind")
    identity = entry.get("identity")
    if not isinstance(identity, dict) or identity.get("origin") not in ("Recording", "Received"):
        raise ValueError("invalid journal identity")
    session_id = identity.get("sessionId")
    if not isinstance(session_id, str) or str(UUID(session_id)) != session_id.lower():
        raise ValueError("invalid journal identity")
    session = entry.get("session")
    if session is not None and layout.resolve(session) != layout.session(identity["origin"], session_id):
        raise ValueError("journal session is not this meeting's folder")
    _basename(entry.get("oldPath"))
    _basename(entry.get("newPath"))
    if entry["kind"] == "delete":
        if entry.get("phase") != "intent":
            raise ValueError("invalid delete phase")
        _revision(entry.get("baseRevision"), optional=True)
        return
    if entry.get("phase") not in ("prepared", "staged", "conflict"):
        raise ValueError("invalid journal phase")
    if entry.get("stage") != f"{META}/staging/{entry['op']}.opus.stage":
        raise ValueError("invalid journal stage")
    _revision(entry.get("baseRevision"))
    _revision(entry.get("newRevision"))
    _revision(entry.get("fileRevision"), optional=True)
    if not isinstance(entry.get("meetingUpdate"), dict):
        raise ValueError("invalid meeting update")
    spec = entry.get("name")
    named = isinstance(spec, dict) and all(isinstance(spec.get(k), str) for k in ("prefix", "title", "untitled"))
    if (entry["kind"] != "replace" or spec is not None) and not named:
        raise ValueError("invalid journal name")
    if entry["kind"] in ("replace", "rename"):
        _basename(entry.get("oldPath"), optional=False)
    rejected = entry.get("rejected", [])
    if not isinstance(rejected, list) or not all(isinstance(n, str) for n in rejected):
        raise ValueError("invalid rejected names")
    temporary = entry.get("caseTemp")
    if temporary is not None and (temporary != f".cassini-case-{entry['op']}.opus"):
        raise ValueError("invalid case-rename temporary")


@dataclass
class Report:
    completed: list = field(default_factory=list)  # op ids that finished or were safely discarded
    kept: list = field(default_factory=list)  # (op id or file name, reason): still pending, never deleted
    orphans: list = field(default_factory=list)  # staging files no entry names; kept

    def blocks(self, origin, session_id):
        return any(
            isinstance(item, dict) and item.get("identity") == dict(origin=origin, sessionId=session_id)
            for item, _reason in self.kept
        )


def replay(layout, *, identity=None):
    """Resume pending entries (the caller holds the lock). identity=(origin, sessionId) limits it."""
    from . import publish

    report = Report()
    found = pending(layout)
    report.kept.extend((name, "damaged journal entry") for name in found.damaged)
    for entry in found.entries:
        if identity is not None and entry.get("identity") != dict(origin=identity[0], sessionId=identity[1]):
            continue
        try:
            publish.resume(layout, entry)
        except (publish.Kept, OSError, ValueError, KeyError, TypeError) as error:
            report.kept.append((entry, str(error)))
            continue
        report.completed.append(entry["op"])
    if identity is None:
        named = {entry["op"] for entry in pending(layout).entries}
        try:
            staged = sorted(os.listdir(layout.staging))
        except FileNotFoundError:
            staged = []
        groups = {}
        for name in staged:
            groups.setdefault(name.split(".", 1)[0], []).append(name)
        for op, files in groups.items():
            if op in named:
                continue
            markers = [n for n in (f"{op}.{STARTING}", f"{op}.{BUILD_MARKER}") if n in files]
            if markers:
                if all(abandoned_build(layout, marker) for marker in markers):
                    for name in sorted(files, key=lambda n: n in markers):  # markers last
                        (layout.staging / name).unlink(missing_ok=True)
                continue  # a live build: left alone
            report.orphans.extend(files)
        fsutil.fsync_dir(layout.staging)
    return report


BUILD_MARKER = "build.partial"
STARTING = "new.partial"  # the marker's name until it is locked; both are locked while a build runs


class Build:
    """A stage being built without the Library lock (§11 step-3a Q10).

    `.cassini/staging/<op>.build.partial` is flock'ed for the whole build. Recovery
    removes the files of an unjournaled op only when that marker is no longer locked,
    i.e. its builder died: nothing from it was published and its sources are intact.
    """

    def __init__(self, layout):
        layout.ensure()
        self.layout = layout
        self.op = str(uuid4())
        self.stage = layout.staging / f"{self.op}.opus.stage"
        starting = layout.staging / f"{self.op}.{STARTING}"
        self.fd = os.open(starting, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        os.rename(starting, layout.staging / f"{self.op}.{BUILD_MARKER}")

    def close(self):
        """Release the build. Its files go unless a journal entry now owns the stage."""
        marker = f"{self.op}.{BUILD_MARKER}"
        try:
            owned = (self.layout.journal / f"{self.op}.json").exists()
            for name in os.listdir(self.layout.staging):
                if name.split(".", 1)[0] == self.op and name != marker and (not owned or name.endswith(".partial")):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(self.layout.staging / name)
            # The marker goes last, so recovery never sees this build's files without it.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.layout.staging / marker)
        finally:
            os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def abandoned_build(layout, marker):
    """True when nobody holds the lock on this build marker (its builder is gone)."""
    try:
        fd = os.open(layout.staging / marker, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def recover(root):
    """Startup recovery (§7): take the lock and replay every pending entry."""
    layout = Layout(Path(root))
    with LibraryLock(layout):
        return replay(layout)
