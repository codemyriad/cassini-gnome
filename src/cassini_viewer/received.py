# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy a received Opus into the Library; publish last and deduplicate full bytes."""

import hashlib
import json
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
from .library_schema import valid
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
    requested_root = root
    root = library_root(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with IMPORT_LOCK:
        stage = Path(tempfile.mkdtemp(prefix=".received-", dir=root))
        try:
            # The shared folder schema uses a lowercase .opus copy name even
            # for an .ogg or uppercase source. Keep the Opus bytes untouched.
            stem = original.stem.replace("\\", "-").strip(".") or "recording"
            # Linux NAME_MAX is in bytes; the Library schema also limits the
            # resulting basename. Truncate only the copy name, never the audio.
            stem = stem.encode("utf-8").decode("utf-8")
            copy_name = stem.encode("utf-8")[:250].decode("utf-8", errors="ignore") + ".opus"
            copied = stage / copy_name
            with regular_input(original) as source, copied.open("xb") as output:
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
            candidates = list(root.iterdir())
            if requested_root is None:
                from .library_location import load

                state = load()
                current = Path(state["primary"])
                candidates.extend(current.iterdir() if current.exists() else ())
                candidates.extend(Path(e["source"]) for e in state["pending"])
            for directory in dict.fromkeys(candidates):
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
                dict(kind="embedded", file=copy_name, transcriptId=embedded.default_transcript_id)
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
                received=dict(originalName=copy_name, importedAtMs=time.time_ns() // 1000000, sha256=sha),
            )
            folder = original.parent.name
            if folder and folder not in (".", "..") and "\\" not in folder and len(folder) <= 255:
                try:
                    folder.encode("utf-8")
                except UnicodeEncodeError:
                    pass
                else:
                    metadata["received"]["originalFolderName"] = folder
            if not valid(metadata):
                raise ValueError("Received recording metadata is invalid")
            if (
                len(json.dumps(metadata, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) + 1
                > 1024**2
            ):
                raise ValueError("Received recording metadata exceeds limit")
            atomic_json(stage / "meeting.json", metadata)
            published = _publish_import(stage, identifier, requested_root)
            try:
                return _meeting(published)
            except BaseException:
                # A failed first scan must not leave an invisible, undedupable
                # Received folder behind. This UUID was created by this import.
                shutil.rmtree(published)
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def _meeting(directory):
    meeting = next(
        (
            m
            for m in LibraryScanner(directory.parent).scan(changed_directories={directory}).meetings
            if m.directory == directory
        ),
        None,
    )
    if meeting is None:
        raise ValueError("Imported recording could not be read")
    return meeting


def _publish_import(stage, identifier, root):
    from contextlib import nullcontext

    from .library_location import routing
    from .library_move import _copy_file

    current = stage
    try:
        while True:
            target_root = library_root(root).resolve()
            target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if current.parent != target_root:
                replacement = Path(tempfile.mkdtemp(prefix=".received-", dir=target_root))
                try:
                    shutil.copytree(current, replacement, dirs_exist_ok=True, copy_function=_copy_file)
                except BaseException:
                    shutil.rmtree(replacement)
                    raise
                shutil.rmtree(current)
                current = replacement
            with routing() if root is None else nullcontext():
                if library_root(root).resolve() != target_root:
                    continue
                published = target_root / ("Received-" + identifier)
                current.rename(published)
                fd = os.open(target_root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return published
    finally:
        if current.exists():
            shutil.rmtree(current)
