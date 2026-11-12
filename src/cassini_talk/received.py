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
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import portable
from .export_audio import check_cancel
from .library import LibraryScanner, library_root
from .library_schema import valid
from .meeting_store import atomic_json, read_metadata
from .open_meeting import load_open_meeting

IMPORT_LOCK = threading.Lock()


@dataclass(frozen=True)
class StagedImport:
    stage: Path
    copied: Path
    identifier: str
    requested_root: Path | None
    sha: str
    expected_key: str


def sha256_file(path, cancel=None):
    digest = hashlib.sha256()
    with regular_input(path) as stream:
        before = os.fstat(stream.fileno())
        remaining = before.st_size
        while remaining:
            check_cancel(cancel)
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("Recording changed while being imported")
            remaining -= len(block)
            digest.update(block)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Recording changed while being imported")
    return digest.hexdigest()


def regular_input(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("Recording is not a regular file")
    return os.fdopen(descriptor, "rb")


def import_received(path, root=None, *, cancel=None):
    return import_one(path, root, cancel=cancel)[0]


def _index_published(dedup_index, sha, published):
    if dedup_index is None:
        return
    resolved = published.resolve()
    dedup_index.setdefault("seen_dirs", set()).add(resolved)
    bucket = dedup_index.setdefault("by_sha", {}).setdefault(sha, [])
    if resolved not in bucket:
        bucket.append(resolved)


def _dedup_candidates(root, requested_root, sha, cancel, dedup_index):
    candidates = list(root.iterdir())
    if requested_root is None:
        from .library_location import load

        state = load()
        primary = Path(state["primary"]).resolve()
        pending = tuple(Path(e["source"]).resolve() for e in state["pending"])
        candidates.extend(primary.iterdir() if primary.exists() else ())
        candidates.extend(pending)
    unique_candidates = list(dict.fromkeys(candidates))
    if dedup_index is None:
        return unique_candidates
    seen_dirs = dedup_index.setdefault("seen_dirs", set())
    by_sha = dedup_index.setdefault("by_sha", {})
    for directory in unique_candidates:
        check_cancel(cancel)
        if directory in seen_dirs:
            continue
        if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith("Received-"):
            continue
        seen_dirs.add(directory)
        try:
            value = read_metadata(directory)
            by_sha.setdefault(value["received"]["sha256"], []).append(directory)
        except (OSError, ValueError, KeyError):
            continue
    return list(by_sha.get(sha, ()))


def _find_duplicate(candidates, sha, cancel):
    for directory in candidates:
        check_cancel(cancel)
        if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith("Received-"):
            continue
        try:
            value = read_metadata(directory)
            received = value["received"]
            current = directory / received["originalName"]
            if received["sha256"] == sha and not current.is_symlink() and sha256_file(current, cancel) == sha:
                return _meeting(directory)
        except (OSError, ValueError, KeyError):
            continue
    return None


def _prepare_one(
    path,
    root=None,
    *,
    cancel=None,
    expected_bytes=None,
    original_folder=None,
    dedup_index=None,
    staged_by_sha=None,
    defer_publish=False,
):
    check_cancel(cancel)
    original = Path(path).absolute()
    if (
        original.suffix.lower() not in (".opus", ".ogg")
        or not original.name.rsplit(".", 1)[0].strip(".")
        or not original.is_file()
    ):
        raise ValueError("Choose an Ogg Opus recording")
    requested_root = root
    root = library_root(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with IMPORT_LOCK:
        stage = Path(tempfile.mkdtemp(prefix=".received-", dir=root))
        keep_stage = False
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
                if expected_bytes is not None and before.st_size != expected_bytes:
                    raise ValueError("Recording changed while being imported")
                written = 0
                os.chmod(copied, 0o600)
                digest = hashlib.sha256()
                while written < before.st_size:
                    check_cancel(cancel)
                    block = source.read(min(1024 * 1024, before.st_size - written))
                    if not block:
                        raise ValueError("Recording changed while being imported")
                    check_cancel(cancel)
                    written += len(block)
                    if expected_bytes is not None and written > expected_bytes:
                        raise ValueError("Recording changed while being imported")
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
            candidates = _dedup_candidates(root, requested_root, sha, cancel, dedup_index)
            existing = _find_duplicate(candidates, sha, cancel)
            if existing is not None:
                return existing, False, None
            prior_staged = staged_by_sha.get(sha) if staged_by_sha is not None else None
            if (
                prior_staged is not None
                and prior_staged.copied.is_file()
                and sha256_file(prior_staged.copied, cancel) == sha
            ):
                return None, False, prior_staged
            check_cancel(cancel)
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
            folder = original.parent.name if original_folder is None else original_folder
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
            check_cancel(cancel)
            if defer_publish:
                expected_key = str(library_root(requested_root).resolve() / ("Received-" + identifier))
                staged = StagedImport(
                    stage=stage,
                    copied=copied,
                    identifier=identifier,
                    requested_root=requested_root,
                    sha=sha,
                    expected_key=expected_key,
                )
                if staged_by_sha is not None:
                    staged_by_sha[sha] = staged
                keep_stage = True
                return None, True, staged
            published = (
                _publish_import(stage, identifier, requested_root)
                if cancel is None
                else _publish_import(stage, identifier, requested_root, cancel=cancel)
            )
            try:
                meeting = _meeting(published)
            except BaseException:
                # A failed first scan must not leave an invisible, undedupable
                # Received folder behind. This UUID was created by this import.
                shutil.rmtree(published)
                raise
            _index_published(dedup_index, sha, published)
            return meeting, True, None
        finally:
            if not keep_stage and stage.exists():
                shutil.rmtree(stage)


def import_one(path, root=None, *, cancel=None, expected_bytes=None, original_folder=None, dedup_index=None):
    """Return (meeting, newly_imported), with chunk cancellation and exact-byte dedup."""
    meeting, added, _ = _prepare_one(
        path,
        root,
        cancel=cancel,
        expected_bytes=expected_bytes,
        original_folder=original_folder,
        dedup_index=dedup_index,
        defer_publish=False,
    )
    return meeting, added


def stage_one(
    path,
    root=None,
    *,
    cancel=None,
    expected_bytes=None,
    original_folder=None,
    dedup_index=None,
    staged_by_sha=None,
):
    """Stage an archive member privately until whole-archive validation completes."""
    return _prepare_one(
        path,
        root,
        cancel=cancel,
        expected_bytes=expected_bytes,
        original_folder=original_folder,
        dedup_index=dedup_index,
        staged_by_sha=staged_by_sha,
        defer_publish=True,
    )


def commit_staged(staged_item, *, dedup_index=None):
    """Publish a privately staged archive item, or deduplicate if published concurrently."""
    with IMPORT_LOCK:
        try:
            root = library_root(staged_item.requested_root).resolve()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            candidates = _dedup_candidates(root, staged_item.requested_root, staged_item.sha, None, dedup_index)
            existing = _find_duplicate(candidates, staged_item.sha, None)
            if existing is not None:
                return existing, False
            published = _publish_import(staged_item.stage, staged_item.identifier, staged_item.requested_root)
            try:
                meeting = _meeting(published)
            except BaseException:
                shutil.rmtree(published)
                raise
            _index_published(dedup_index, staged_item.sha, published)
            return meeting, True
        finally:
            if staged_item.stage.exists():
                shutil.rmtree(staged_item.stage, ignore_errors=True)


def discard_staged(staged_items):
    with IMPORT_LOCK:
        for item in staged_items:
            if item.stage.exists():
                shutil.rmtree(item.stage, ignore_errors=True)


def _meeting(directory):
    meeting = next(
        (
            m
            for m in LibraryScanner(directory.parent, include_published=False)
            .scan(changed_directories={directory})
            .meetings
            if m.directory == directory
        ),
        None,
    )
    if meeting is None:
        raise ValueError("Imported recording could not be read")
    return meeting


def _publish_import(stage, identifier, root, *, cancel=None):
    from contextlib import nullcontext

    from .library_location import routing
    from .library_move import _copy_file

    current = stage
    try:
        while True:
            check_cancel(cancel)
            target_root = library_root(root).resolve()
            target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if current.parent != target_root:
                replacement = Path(tempfile.mkdtemp(prefix=".received-", dir=target_root))
                try:
                    shutil.copytree(
                        current,
                        replacement,
                        dirs_exist_ok=True,
                        copy_function=lambda source, target: _copy_file(source, target, cancelled=cancel),
                    )
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
