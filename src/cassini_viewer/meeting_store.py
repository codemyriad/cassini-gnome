# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic app-owned metadata; capture audio and session.json remain read-only."""

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from .library import meeting_id
from .library_files import regular
from .library_schema import valid
from .recording_session import strict_json

LOCK = threading.RLock()
_TRANSACTIONS = threading.local()


@contextlib.contextmanager
def metadata_transaction(directory):
    """Serialize an entire metadata edit across GTK/broker processes.

    The lock lives beside meeting.json, whose inode changes on every edit.
    Nested edits on one thread reuse the same flock, including path aliases.
    Readers still read atomic snapshots without acquiring a write lock.
    """
    with LOCK:
        directory = Path(directory)
        if not directory.exists():
            from .library_location import owned_directory

            directory = owned_directory(directory)
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(parent)
            key = info.st_dev, info.st_ino
            held = getattr(_TRANSACTIONS, "held", None)
            if held is None:
                held = _TRANSACTIONS.held = {}
            if key in held:
                yield
                return
            descriptor = os.open(
                ".meeting.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("Unsafe meeting metadata lock")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                held[key] = descriptor
                try:
                    yield
                finally:
                    held.pop(key)
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)


def atomic_json(path, value, *, before_commit=None):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="." + path.name + "-", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if before_commit is not None:
            before_commit()
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_metadata(directory):
    directory = Path(directory)
    identifier = meeting_id(Path(directory))
    if identifier is None:
        raise ValueError("Meeting is outside the Library")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            with regular(descriptor, "meeting.json") as (stream, _info):
                raw = stream.read(1024 * 1024 + 1)
        except FileNotFoundError:
            raw = None
    finally:
        os.close(descriptor)
    if raw is not None:
        if len(raw) > 1024 * 1024:
            raise ValueError("Meeting metadata exceeds limit")
        value = strict_json(raw)
        if not valid(value) or value["id"] != identifier:
            raise ValueError("Cannot edit invalid or newer meeting metadata")
        if directory.name.startswith("Received-") != (value["received"] is not None):
            raise ValueError("Invalid received metadata")
        return value
    if directory.name.startswith("Received-"):
        raise ValueError("Received meeting is not published")
    return dict(
        version=1,
        id=identifier,
        revision=0,
        title=None,
        titleSource="meeting-window",
        notes=[],
        speakers=[],
        transcript=dict(status="none", reference=None),
        etaMinutes=None,
        error=None,
        quoteOverride=None,
        received=None,
    )


def update_metadata(directory, *, before_commit=None, **changes):
    directory = Path(directory)
    with metadata_transaction(directory):
        if not directory.exists():
            from .library_location import owned_directory

            with contextlib.suppress(ValueError):
                directory = owned_directory(directory)
        value = read_metadata(directory)
        value.update(changes)
        value["revision"] += 1
        if not valid(value):
            raise ValueError("Invalid app-owned metadata update")
        if before_commit is None:
            atomic_json(directory / "meeting.json", value)
        else:
            atomic_json(directory / "meeting.json", value, before_commit=before_commit)
        return value
