# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy a received Opus into the Library; publish last and deduplicate full bytes."""

import hashlib
import os
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from . import portable
from .library import LibraryScanner, library_root
from .meeting_store import atomic_json, read_metadata
from .open_meeting import load_open_meeting

IMPORT_LOCK = threading.Lock()


def sha256_file(path):
    with regular_input(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular_input(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("Recording is not a regular file")
    return os.fdopen(descriptor, "rb")


def import_received(path, root=None):
    original = Path(path).absolute()
    if original.suffix.lower() not in (".opus", ".ogg") or not original.is_file():
        raise ValueError("Choose an Ogg Opus recording")
    root = library_root(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with IMPORT_LOCK:
        stage = Path(tempfile.mkdtemp(prefix=".received-", dir=root))
        try:
            copied = stage / original.name
            with regular_input(original.resolve(strict=True)) as source, copied.open("xb") as output:
                before = os.fstat(source.fileno())
                os.chmod(copied, 0o600)
                digest = hashlib.sha256()
                while block := source.read(1024 * 1024):
                    digest.update(block)
                    output.write(block)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Recording changed while being imported")
                output.flush()
                os.fsync(output.fileno())
            sha = digest.hexdigest()
            # A stored digest is an index. A changed/missing original copy must
            # not prevent this good incoming copy from reaching the Library.
            for directory in root.iterdir():
                if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith("Received-"):
                    continue
                try:
                    value = read_metadata(directory)
                    received = value["received"]
                    current = directory / received["originalName"]
                    if received["sha256"] == sha and not current.is_symlink() and sha256_file(current) == sha:
                        return _meeting(directory)
                except (OSError, ValueError, KeyError):
                    continue
            opened = load_open_meeting(copied, root)
            if not opened.has_audio:
                raise ValueError("The recording has no complete playable Opus stream")
            embedded = portable.read_meeting(str(copied))
            status = opened.transcript
            reference = (
                dict(kind="embedded", file=original.name, transcriptId=embedded.default_transcript_id)
                if status == "done"
                else None
            )
            identifier = str(uuid4())
            metadata = dict(
                version=1,
                id=identifier,
                revision=0,
                title=None,
                titleSource="meeting-window",
                notes=list(opened.notes),
                speakers=[],
                transcript=dict(status=status, reference=reference),
                etaMinutes=None,
                error=dict(id="transcript-damaged", action=None) if status == "damaged" else None,
                quoteOverride=None,
                received=dict(originalName=original.name, importedAtMs=time.time_ns() // 1000000, sha256=sha),
            )
            if original.parent.name:
                metadata["received"]["originalFolderName"] = original.parent.name
            atomic_json(stage / "meeting.json", metadata)
            published = root / ("Received-" + identifier)
            stage.rename(published)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return _meeting(published)
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def _meeting(directory):
    return next(
        m
        for m in LibraryScanner(directory.parent).scan(changed_directories={directory}).meetings
        if m.directory == directory
    )
