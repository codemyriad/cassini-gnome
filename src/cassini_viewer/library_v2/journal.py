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

import errno
import fcntl
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

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


def new_entry(kind, *, origin, session_id, **members):
    if kind not in KINDS:
        raise ValueError("unknown journal kind")
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return dict(
        op=str(uuid4()), kind=kind, createdAt=now, identity=dict(origin=origin, sessionId=session_id), **members
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


def pending(layout):
    """Committed entries, oldest first. A leftover `.json.partial` was never committed, so no copy
    was touched after it: it is removed. Unreadable entries are kept and reported."""
    result = Pending()
    try:
        names = sorted(os.listdir(layout.journal))
    except FileNotFoundError:
        return result
    for name in names:
        path = layout.journal / name
        if name.endswith(".json.partial"):
            path.unlink(missing_ok=True)
            continue
        if not name.endswith(".json"):
            continue
        try:
            with open(path, "rb") as stream:
                raw = stream.read(MAX_ENTRY + 1)
            entry = strict_json(raw) if len(raw) <= MAX_ENTRY else None
            if (
                not isinstance(entry, dict)
                or entry.get("kind") not in KINDS
                or name != f"{entry.get('op')}.json"
                or _path(layout, entry["op"]) != path
            ):
                raise ValueError("invalid-journal-entry")
        except (OSError, ValueError):
            result.damaged.append(name)
            continue
        result.entries.append(entry)
    result.entries.sort(key=lambda e: (str(e.get("createdAt")), e["op"]))
    return result


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
        except (publish.Kept, OSError, ValueError) as error:
            report.kept.append((entry, str(error)))
            continue
        report.completed.append(entry["op"])
    if identity is None:
        named = set()
        for entry in pending(layout).entries:
            named.add(entry["op"])
        try:
            staged = sorted(os.listdir(layout.staging))
        except FileNotFoundError:
            staged = []
        report.orphans = [n for n in staged if n.split(".", 1)[0] not in named]
    return report


def recover(root):
    """Startup recovery (§7): take the lock and replay every pending entry."""
    layout = Layout(Path(root))
    with LibraryLock(layout):
        return replay(layout)
