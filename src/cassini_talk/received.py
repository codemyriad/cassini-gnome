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

from . import import_types, portable
from .audio_import import decode_to_opus
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
    # Full-file SHA-256 of a decoded plain-audio original (MP3/M4A/WAV).
    source_sha: str | None = None

    @property
    def dedup_sha(self):
        return self.source_sha or self.sha


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
            source_sha = value["received"].get("sourceSha256")
            if isinstance(source_sha, str):
                by_sha.setdefault(source_sha, []).append(directory)
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
            # A decoded import matches on its original's digest while its Opus
            # copy must still hold the recorded Library digest.
            if sha not in (received["sha256"], received.get("sourceSha256")) or current.is_symlink():
                continue
            if sha256_file(current, cancel) == received["sha256"]:
                return _meeting(directory)
        except (OSError, ValueError, KeyError):
            continue
    return None


def _document_portal_roots():
    roots = [Path("/run/flatpak/doc"), Path(f"/run/user/{os.getuid()}/doc")]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        roots.append(Path(runtime) / "doc")
    return roots


def source_folder_name(original):
    """The folder a file was opened from, or None when only a document-portal id is visible."""
    parent = original.parent
    if any(parent.parent == root for root in _document_portal_roots()):
        return None
    return parent.name


def _copy_opus(original, copied, expected_bytes, cancel):
    """Copy Opus/Ogg bytes untouched; ambiguous .ogg must prove an Ogg Opus stream."""
    with regular_input(original) as source, copied.open("xb") as output:
        before = os.fstat(source.fileno())
        if expected_bytes is not None and before.st_size != expected_bytes:
            raise ValueError("Recording changed while being imported")
        if import_types.needs_ogg_probe(original.name) and import_types.check_ogg_file(source) is not None:
            raise ValueError("Choose an Ogg Opus recording")
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
    return digest.hexdigest()


def _hash_source(original, expected_bytes, cancel):
    """Full-file SHA-256 of a plain-audio original (the dedup key) and its (size, mtime)."""
    with regular_input(original) as source:
        before = os.fstat(source.fileno())
    if expected_bytes is not None and before.st_size != expected_bytes:
        raise ValueError("Recording changed while being imported")
    sha = sha256_file(original, cancel)
    after = os.stat(original, follow_symlinks=False)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Recording changed while being imported")
    return sha, (after.st_size, after.st_mtime_ns)


def _lookup_duplicate(root, requested_root, sha, cancel, dedup_index, staged_by_sha):
    """(published duplicate, staged duplicate) for a dedup key; either may be None."""
    # A stored digest is an index. A changed/missing original copy must
    # not prevent this good incoming copy from reaching the Library.
    candidates = _dedup_candidates(root, requested_root, sha, cancel, dedup_index)
    existing = _find_duplicate(candidates, sha, cancel)
    if existing is not None:
        return existing, None
    prior = staged_by_sha.get(sha) if staged_by_sha is not None else None
    if prior is None or not prior.copied.is_file():
        return None, None
    if prior.source_sha is not None:
        return None, (prior if prior.source_sha == sha else None)
    return None, (prior if sha256_file(prior.copied, cancel) == sha else None)


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
        import_types.entry_for(original.name) is None
        or not original.name.rsplit(".", 1)[0].strip(".")
        or not original.is_file()
    ):
        raise ValueError("Choose an Ogg Opus recording")
    # L-25 / W4.3: MP3, M4A and WAV are decoded into an Opus copy; the original
    # file is only read. Opus/Ogg bytes are copied untouched.
    plain = import_types.is_plain_audio(original.name)
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
            if plain:
                source_sha, source_stat = _hash_source(original, expected_bytes, cancel)
                existing, prior = _lookup_duplicate(
                    root, requested_root, source_sha, cancel, dedup_index, staged_by_sha
                )
                if existing is not None:
                    return existing, False, None
                if prior is not None:
                    return None, False, prior
                check_cancel(cancel)
                # Rejects a truncated container and fsyncs the Opus copy, so the
                # durable meeting.json below never points at incomplete audio.
                decode_to_opus(original, copied, cancel=cancel)
                os.chmod(copied, 0o600)
                after = os.stat(original, follow_symlinks=False)
                if source_stat != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Recording changed while being imported")
                sha = sha256_file(copied, cancel)
                dedup_sha = source_sha
            else:
                sha = _copy_opus(original, copied, expected_bytes, cancel)
                dedup_sha = sha
                existing, prior = _lookup_duplicate(root, requested_root, sha, cancel, dedup_index, staged_by_sha)
                if existing is not None:
                    return existing, False, None
                if prior is not None:
                    return None, False, prior
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
            if plain:
                # sha256 stays the Library copy's digest (LIBRARY.md); the
                # original's digest lets a re-import of the same file dedupe.
                metadata["received"]["sourceSha256"] = dedup_sha
            folder = source_folder_name(original) if original_folder is None else original_folder
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
                    source_sha=dedup_sha if plain else None,
                )
                if staged_by_sha is not None:
                    staged_by_sha[dedup_sha] = staged
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
            _index_published(dedup_index, dedup_sha, published)
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
            key = staged_item.dedup_sha
            candidates = _dedup_candidates(root, staged_item.requested_root, key, None, dedup_index)
            existing = _find_duplicate(candidates, key, None)
            if existing is not None:
                return existing, False
            published = _publish_import(staged_item.stage, staged_item.identifier, staged_item.requested_root)
            try:
                meeting = _meeting(published)
            except BaseException:
                shutil.rmtree(published)
                raise
            _index_published(dedup_index, key, published)
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
