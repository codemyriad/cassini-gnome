# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 import-side transaction: streaming, cancellable, bounded per archive and batch."""

import tempfile
import zipfile
import zlib
from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from . import archive_import
from .export_audio import check_cancel
from .received import commit_staged, discard_staged, import_one, regular_input, stage_one

DECODE_ERRORS = (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error)

SUPPORTED = {".opus", ".ogg"}
# The common ZIP dialect used by Android/iOS and D96 exports. LZMA/Zstd
# decoders can allocate an attacker-declared dictionary before yielding bytes.
METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def supported(entry):
    if archive_import.is_directory(entry) or archive_import.is_macos_metadata(entry):
        return False
    norm = archive_import.normalized_entry_path(archive_import.entry_name(entry))
    return PurePosixPath(norm).suffix.lower() in SUPPORTED and entry.compress_type in METHODS


@dataclass
class Budget:
    entries: int = field(default_factory=lambda: archive_import.MAX_ENTRIES)
    bytes: int = field(default_factory=lambda: archive_import.MAX_BYTES)
    ratio: int = field(default_factory=lambda: archive_import.MAX_RATIO)
    read_bytes: int = 0


@dataclass
class Result:
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    ignored: int = 0
    failed: int = 0
    cancelled: bool = False
    last_key: str | None = None
    # Bounded by entries; codes/filenames only, never transcript/model objects.
    errors: list[tuple[str, str]] = field(default_factory=list)


def import_files(paths, root=None, *, cancel=None, progress=None):
    """Use on a worker. Keep successful files on cancellation or another failure.

    Only ZIP and currently supported Opus inputs are handled. Nested archives
    are ignored and counted in unsupported. Private extraction disappears on
    every exit; archive members stay privately staged until whole-archive
    validation completes so rollback never touches published Library documents.
    """
    result, batch_budget = Result(), Budget()
    dedup_index = {}

    def report():
        if progress is not None:
            progress(replace(result, errors=list(result.errors)))

    def failed(name, reason):
        result.failed += 1
        result.errors.append((str(name), reason))

    def import_path(path, expected_bytes, original_folder=None):
        check_cancel(cancel)
        meeting, added = import_one(
            path,
            root,
            cancel=cancel,
            expected_bytes=expected_bytes,
            original_folder=original_folder,
            dedup_index=dedup_index,
        )
        if added:
            result.imported += 1
        else:
            result.duplicates += 1
        result.last_key = meeting.key
        return meeting, added

    try:
        selected = []
        for path in paths:
            check_cancel(cancel)
            if len(selected) >= archive_import.MAX_ENTRIES:
                failed("", "limit")
                break
            selected.append(path)
        result.total = len(selected) + result.failed
        report()
        for index, path in enumerate(selected):
            check_cancel(cancel)
            if index >= archive_import.MAX_ENTRIES:
                failed("", "limit")
                break
            if path is None:
                failed("", "unavailable")
                report()
                continue
            path = Path(path)
            try:
                if path.suffix.lower() == ".zip":
                    archive_budget = Budget()
                    saved_state = (
                        result.total,
                        result.imported,
                        result.duplicates,
                        result.ignored,
                        result.failed,
                        result.last_key,
                        len(result.errors),
                    )
                    staged_archive = []
                    staged_by_sha = {}

                    def commit_archive(pending=staged_archive):
                        while pending:
                            item, norm_name = pending.pop(0)
                            try:
                                meeting, added = commit_staged(item, dedup_index=dedup_index)
                            except DECODE_ERRORS:
                                result.imported -= 1
                                failed(norm_name, "damaged")
                                continue
                            if not added:
                                result.imported -= 1
                                result.duplicates += 1
                            if result.last_key == item.expected_key or result.last_key is None:
                                result.last_key = meeting.key

                    try:
                        with regular_input(path) as incoming:
                            cd_offset = archive_import.preflight(incoming)
                            with zipfile.ZipFile(incoming) as archive:
                                entries = archive_import.entries(archive, archive_budget, cd_offset=cd_offset)
                                files = [entry for entry in entries if not archive_import.is_directory(entry)]
                                result.total += len(files) - 1
                                if not any(supported(entry) for entry in files):
                                    result.errors.append((path.name, "empty"))
                                report()
                                for entry in entries:
                                    check_cancel(cancel)
                                    if archive_import.is_directory(entry):
                                        archive_import.drain_ignored(archive, entry, cancel, budget=archive_budget)
                                        continue
                                    if not supported(entry):
                                        archive_import.drain_ignored(archive, entry, cancel, budget=archive_budget)
                                        result.ignored += 1
                                        report()
                                        continue
                                    norm_name = archive_import.normalized_entry_path(archive_import.entry_name(entry))
                                    try:
                                        with tempfile.TemporaryDirectory(prefix="cassini-zip-import-") as folder:
                                            extracted = archive_import.extract(
                                                archive, entry, folder, cancel, budget=archive_budget
                                            )
                                            parent = PurePosixPath(norm_name).parent.name or path.stem
                                            meeting, added, staged_item = stage_one(
                                                extracted,
                                                root,
                                                cancel=cancel,
                                                expected_bytes=entry.file_size,
                                                original_folder=parent,
                                                dedup_index=dedup_index,
                                                staged_by_sha=staged_by_sha,
                                            )
                                            if added:
                                                staged_archive.append((staged_item, norm_name))
                                                result.imported += 1
                                                result.last_key = staged_item.expected_key
                                            else:
                                                result.duplicates += 1
                                                result.last_key = (
                                                    meeting.key if meeting is not None else staged_item.expected_key
                                                )
                                    except (CancelledError, archive_import.ArchiveError):
                                        raise
                                    except DECODE_ERRORS:
                                        failed(norm_name, "damaged")
                                    report()
                        commit_archive()
                    except CancelledError:
                        commit_archive()
                        raise
                    except archive_import.ArchiveError:
                        discard_staged([item for item, _ in staged_archive])
                        staged_archive.clear()
                        (
                            result.total,
                            result.imported,
                            result.duplicates,
                            result.ignored,
                            result.failed,
                            result.last_key,
                        ) = saved_state[:6]
                        del result.errors[saved_state[6] :]
                        raise
                    finally:
                        if staged_archive:
                            discard_staged([item for item, _ in staged_archive])
                            staged_archive.clear()
                elif path.suffix.lower() in SUPPORTED:
                    with regular_input(path) as source:
                        size = source.seek(0, 2)
                    if batch_budget.entries < 1 or size > batch_budget.bytes:
                        raise archive_import.ArchiveError("limit")
                    batch_budget.entries -= 1
                    batch_budget.bytes -= size
                    import_path(path, size)
                else:
                    if batch_budget.entries < 1:
                        raise archive_import.ArchiveError("limit")
                    batch_budget.entries -= 1
                    result.ignored += 1
            except archive_import.ArchiveError as error:
                failed(path.name, error.reason)
            except DECODE_ERRORS:
                failed(path.name, "damaged")
            report()
    except CancelledError:
        result.cancelled = True
        report()
    return result
