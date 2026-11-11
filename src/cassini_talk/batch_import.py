# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 import-side transaction: streaming, cancellable, bounded per batch."""

import tempfile
import zipfile
import zlib
from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import archive_import
from .export_audio import check_cancel
from .received import import_one, regular_input

try:
    from lzma import LZMAError
except ImportError:  # zipfile itself reports unsupported LZMA on such builds.
    LZMAError = NotImplementedError

DECODE_ERRORS = (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error, LZMAError)

SUPPORTED = {".opus", ".ogg"}


@dataclass
class Budget:
    entries: int = field(default_factory=lambda: archive_import.MAX_ENTRIES)
    bytes: int = field(default_factory=lambda: archive_import.MAX_BYTES)


@dataclass
class Result:
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
    are ignored. Private extraction disappears on every exit; the existing
    importer owns publication, byte-digest dedup and partial-copy rollback.
    """
    result, budget = Result(), Budget()

    def report():
        if progress is not None:
            progress(replace(result, errors=list(result.errors)))

    def failed(name, reason):
        result.failed += 1
        result.errors.append((str(name), reason))

    def import_path(path, expected_bytes, original_folder=None):
        check_cancel(cancel)
        meeting, added = import_one(
            path, root, cancel=cancel, expected_bytes=expected_bytes, original_folder=original_folder
        )
        if added:
            result.imported += 1
        else:
            result.duplicates += 1
        result.last_key = meeting.key

    try:
        # Do not materialize an unbounded iterable or queue thousands of paths.
        for index, path in enumerate(paths):
            check_cancel(cancel)
            if index >= archive_import.MAX_ENTRIES:
                failed("", "limit")
                break
            path = Path(path)
            try:
                if path.suffix.lower() == ".zip":
                    with regular_input(path) as incoming:
                        archive_import.preflight(incoming)
                        with zipfile.ZipFile(incoming) as archive:
                            entries = archive_import.entries(archive, budget)
                            for entry in entries:
                                check_cancel(cancel)
                                if entry.is_dir():
                                    continue
                                if Path(entry.filename).suffix.lower() not in SUPPORTED:
                                    result.ignored += 1
                                    report()
                                    continue
                                try:
                                    with tempfile.TemporaryDirectory(prefix="cassini-zip-import-") as folder:
                                        extracted = archive_import.extract(archive, entry, folder, cancel)
                                        parent = Path(entry.filename).parent.name or path.stem
                                        import_path(extracted, entry.file_size, parent)
                                except CancelledError:
                                    raise
                                except DECODE_ERRORS:
                                    failed(entry.filename, "damaged")
                                report()
                elif path.suffix.lower() in SUPPORTED:
                    with regular_input(path) as source:
                        size = source.seek(0, 2)
                    if budget.entries < 1 or size > budget.bytes:
                        raise archive_import.ArchiveError("limit")
                    budget.entries -= 1
                    budget.bytes -= size
                    import_path(path, size)
                else:
                    if budget.entries < 1:
                        raise archive_import.ArchiveError("limit")
                    budget.entries -= 1
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
