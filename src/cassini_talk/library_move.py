# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-meeting move primitives; routing/retained roots belong to the application."""

import ctypes
import errno
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .library import meeting_id

BLOCK = 1024 * 1024


@dataclass(frozen=True)
class Moved:
    source: Path
    destination: Path
    cleanup: Path | None = None
    warning: str | None = None


@dataclass(frozen=True)
class MoveFailure:
    directory: Path
    error: str


@dataclass(frozen=True)
class MoveResult:
    moved: tuple[Moved, ...]
    failed: tuple[MoveFailure, ...]
    deferred: tuple[Path, ...]


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename(source, destination):
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        problem = ctypes.get_errno()
        raise OSError(problem, os.strerror(problem), str(destination))


def _snapshot(root):
    result = {}
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in [None, *folders, *names]:
            path = Path(directory) if name is None else Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError("Unsupported file in meeting: " + str(path))
            result[str(path.relative_to(root))] = (
                info.st_mode,
                info.st_ino,
                info.st_size if not stat.S_ISDIR(info.st_mode) else 0,
                info.st_mtime_ns,
                os.readlink(path) if stat.S_ISLNK(info.st_mode) else None,
            )
    return result


def _copy_file(source, target, *, cancelled=None):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Meeting contains a non-regular file")
        expected = hashlib.sha256()
        with open(target, "xb") as output:
            while block := incoming.read(BLOCK):
                _check_cancelled(cancelled)
                output.write(block)
                expected.update(block)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(incoming.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Meeting changed during its move")
    actual = hashlib.sha256()
    with open(target, "rb") as copied:
        while block := copied.read(BLOCK):
            actual.update(block)
    if actual.digest() != expected.digest():
        raise ValueError("Meeting copy verification failed")
    shutil.copystat(source, target, follow_symlinks=False)
    return target


def _check_cancelled(event):
    if event is not None and event.is_set():
        from concurrent.futures import CancelledError

        raise CancelledError()


class MoveBusy(Exception):
    """The meeting acquired a writer before publication; leave it pending."""


def move_folder(source, destination, *, cancelled=None, ready=lambda: True):
    """Never overwrite. Cross-filesystem copies publish only after verification."""
    if not meeting_id(source) or not stat.S_ISDIR(source.lstat().st_mode):
        raise ValueError("Not a direct meeting folder")
    from .meeting_store import LOCK

    target = destination / source.name
    _check_cancelled(cancelled)
    with LOCK:
        if not ready():
            raise MoveBusy()
        try:
            _rename(source, target)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
        else:
            warning = None
            try:
                _sync(destination)
                if source.parent != destination:
                    _sync(source.parent)
            except OSError as error:
                warning = str(error)
            return Moved(source, target, warning=warning)
    before = _snapshot(source)
    required = sum(row[2] for row in before.values() if stat.S_ISREG(row[0]))
    if shutil.disk_usage(destination).free < required + 65536:
        raise OSError(errno.ENOSPC, "Not enough space for the meeting copy")
    stage = Path(tempfile.mkdtemp(prefix=".cassini-moving-", dir=destination))
    published = False
    try:
        from functools import partial

        copy = _copy_file if cancelled is None else partial(_copy_file, cancelled=cancelled)
        shutil.copytree(source, stage, symlinks=True, dirs_exist_ok=True, copy_function=copy)
        with LOCK:
            if not ready():
                raise MoveBusy()
            if _snapshot(source) != before:
                raise ValueError("Meeting changed during its move")
            for directory, _folders, _names in os.walk(stage, topdown=False, followlinks=False):
                _sync(directory)
            _check_cancelled(cancelled)
            _rename(stage, target)
            published = True
            retired = source.with_name(".cassini-moved-" + uuid4().hex)
            try:
                _sync(destination)
                _rename(source, retired)
            except OSError:
                # Source is still complete. Revert this unpublished routing choice.
                shutil.rmtree(target)
                _sync(destination)
                raise
            warning = None
            try:
                _sync(source.parent)
            except OSError as error:
                warning = str(error)
            cleanup = None
            try:
                shutil.rmtree(retired)
            except OSError:
                # The complete destination owns the meeting. A hidden old backup
                # may need later cleanup, but no live/failed row points to fragments.
                cleanup = retired
            return Moved(source, target, cleanup, warning)
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)


def move_meetings(root, destination, *, deferred=()):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    names = {entry.name for entry in root.iterdir() if meeting_id(entry) and not entry.is_symlink() and entry.is_dir()}
    if root == destination:
        return MoveResult((), (), ())
    if any(root / name == destination or root / name in destination.parents for name in names):
        raise ValueError("The destination cannot be inside a meeting")
    destination.mkdir(parents=True, exist_ok=True)
    moved, failed, waiting = [], [], []
    for name in sorted(names):
        source = root / name
        if name in deferred:
            waiting.append(source)
            continue
        try:
            moved.append(move_folder(source, destination))
        except (OSError, ValueError) as error:
            failed.append(MoveFailure(source, str(error)))
    return MoveResult(tuple(moved), tuple(failed), tuple(waiting))
