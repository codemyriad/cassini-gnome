# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 bulk stored-.zip export: one staged meeting at a time, never /tmp PCM."""

import os
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

from . import archive_import, portable
from .archive_import import _validate_entry_name
from .export_audio import check_cancel
from .export_model import file_stem
from .exporting import (
    _fsync_directory,
    _publish,
    _require_export_directory,
    verify_cassini_opus,
    write_cassini_opus,
)
from .portable_context import embedded_notes
from .received import regular_input

CHUNK = 1024 * 1024


def is_exportable(meeting, *, trash=None):
    return bool(
        meeting is not None
        and meeting.lifecycle == "saved"
        and meeting.has_audio
        and not meeting.published_missing
        and (trash is None or not trash.hides(meeting.directory))
    )


def exportable_meetings(snapshot, *, trash=None):
    if snapshot is None or snapshot.unavailable:
        return ()
    return tuple(meeting for meeting in snapshot.meetings if is_exportable(meeting, trash=trash))


def entry_stem(title):
    stem = file_stem(title or "").lstrip(".").strip() or "Untitled meeting"
    if len(stem) >= 2 and stem[0].isascii() and stem[0].isalpha() and stem[1] == ":":
        stem = stem[0] + "-" + stem[2:]
    while len(stem.encode("utf-8")) > 230:
        stem = stem[:-1]
    return stem.strip() or "Untitled meeting"


def unique_entry_name(title, seen, suffix="opus"):
    stem = entry_stem(title)
    number = 1
    while True:
        candidate = f"{stem}{' ' + str(number) if number > 1 else ''}.{suffix}"
        _validate_entry_name(candidate)
        folded = candidate.casefold()
        if folded not in seen:
            seen.add(folded)
            return candidate
        number += 1


def _has_private_library_extension(meeting):
    ours = (meeting.extensions or {}).get("codemyriad.io")
    if isinstance(ours, dict) and ours.get("schema") == 1:
        from .library_v2.xschema import valid

        return valid(ours)
    return False


def _can_copy_verbatim(meeting, embedded):
    if meeting.origin != "received" or meeting.audio_file is None or _has_private_library_extension(meeting):
        return False
    metadata = meeting.metadata or {}
    if metadata.get("title") is not None or metadata.get("titleSource") not in (None, "meeting-window"):
        return False
    if metadata.get("speakers") or metadata.get("speakerMerges") or metadata.get("quoteOverride") is not None:
        return False
    reference = (metadata.get("transcript") or {}).get("reference")
    if reference is not None and reference.get("kind") != "embedded":
        return False
    if embedded.state == portable.State.PLAIN_AUDIO:
        return not meeting.notes and reference is None
    if embedded.state in (portable.State.UNVERIFIED, portable.State.OK):
        return tuple(meeting.notes) == tuple(embedded_notes(embedded))
    return False


def _copy_regular_bounded(source, output, max_bytes, cancel):
    before = os.fstat(source.fileno())
    if before.st_size > max_bytes:
        raise ValueError("Archive exceeds the 20 GB size limit")
    copied = 0
    crc = 0
    while copied < before.st_size:
        check_cancel(cancel)
        block = source.read(min(CHUNK, before.st_size - copied))
        check_cancel(cancel)
        if not block:
            raise ValueError("Recording changed while being exported")
        copied += len(block)
        if copied > max_bytes:
            raise ValueError("Archive exceeds the 20 GB size limit")
        output.write(block)
        crc = zlib.crc32(block, crc)
    after = os.fstat(source.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Recording changed while being exported")
    output.flush()
    os.fsync(output.fileno())
    return copied, crc & 0xFFFFFFFF


def _crc_and_size(path, max_bytes, cancel):
    with regular_input(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > max_bytes:
            raise ValueError("Archive exceeds the 20 GB size limit")
        read_bytes = 0
        crc = 0
        while read_bytes < size:
            check_cancel(cancel)
            block = stream.read(min(CHUNK, size - read_bytes))
            check_cancel(cancel)
            if not block:
                raise ValueError("Export snapshot changed while being verified")
            read_bytes += len(block)
            crc = zlib.crc32(block, crc)
    return size, crc & 0xFFFFFFFF


def _stage_single_meeting(meeting, directory, max_bytes, *, tz=None, cancel=None, is_trashed=None):
    check_cancel(cancel)
    if not is_exportable(meeting) or (is_trashed is not None and is_trashed(meeting.directory)):
        raise ValueError("Meeting is unavailable for export")
    fd, raw_name = tempfile.mkstemp(prefix=".cassini-item-", suffix=".opus", dir=directory)
    item = Path(raw_name)
    try:
        os.chmod(item, 0o600)
        verbatim = False
        if meeting.origin == "received" and meeting.audio_file is not None:
            with regular_input(meeting.audio_file):
                pass
            embedded = portable.read_meeting(str(meeting.audio_file))
            if _can_copy_verbatim(meeting, embedded) and (
                embedded.state == portable.State.PLAIN_AUDIO
                or portable.verify_audio(
                    str(meeting.audio_file), embedded, cancelled=lambda: bool(cancel and cancel.is_set())
                ).state
                == portable.State.OK
            ):
                verbatim = True
        with os.fdopen(fd, "wb") as output:
            fd = None
            if verbatim:
                with regular_input(meeting.audio_file) as source:
                    size, crc = _copy_regular_bounded(source, output, max_bytes, cancel)
                return item, size, crc
            write_cassini_opus(meeting, output, directory, tz=tz, cancel=cancel)
            output.flush()
            os.fsync(output.fileno())
        check_cancel(cancel)
        size, crc = _crc_and_size(item, max_bytes, cancel)
        verify_cassini_opus(item, cancel=cancel)
        return item, size, crc
    except BaseException:
        if fd is not None:
            os.close(fd)
        item.unlink(missing_ok=True)
        raise


def _zip_timestamp():
    now = time.localtime()
    year = max(1980, min(2107, now.tm_year))
    return (year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec)


def _write_stored_entry(archive, archive_stream, entry_name, item, size, crc, cancel):
    info = zipfile.ZipInfo(filename=entry_name, date_time=_zip_timestamp())
    info.compress_type = zipfile.ZIP_STORED
    info.flag_bits = 0x800 if not entry_name.isascii() else 0
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.file_size = size
    info.compress_size = size
    info.CRC = crc
    force_zip64 = size >= 0xFFFFFFFF or archive_stream.tell() >= 0xFFFFFFFF
    copied = 0
    written_crc = 0
    with regular_input(item) as source, archive.open(info, "w", force_zip64=force_zip64) as member:
        while copied < size:
            check_cancel(cancel)
            block = source.read(min(CHUNK, size - copied))
            check_cancel(cancel)
            if not block:
                raise ValueError("Export snapshot truncated while writing archive")
            copied += len(block)
            if copied > size:
                raise ValueError("Export snapshot grew while writing archive")
            written_crc = zlib.crc32(block, written_crc)
            member.write(block)
        if source.read(1):
            raise ValueError("Export snapshot grew while writing archive")
    if copied != size or (written_crc & 0xFFFFFFFF) != crc:
        raise ValueError("Export snapshot changed while writing archive")


def export_meetings_zip(meetings, directory, *, tz=None, cancel=None, progress=None, is_trashed=None):
    meetings = tuple(meetings)
    if not meetings:
        raise ValueError("No meetings selected for export")
    if len({meeting.key for meeting in meetings}) != len(meetings):
        raise ValueError("Duplicate meetings in export")
    if len(meetings) > archive_import.MAX_ENTRIES:
        raise ValueError("Archive exceeds the 2,000-entry limit")
    directory = _require_export_directory(directory)
    check_cancel(cancel)
    stage = None
    try:
        fd, name = tempfile.mkstemp(prefix=".cassini-zip-", dir=directory)
        stage = Path(name)
        os.chmod(stage, 0o600)
        seen_names = set()
        total_bytes = 0
        total = len(meetings)
        with os.fdopen(fd, "wb") as raw_output:
            with zipfile.ZipFile(raw_output, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for index, meeting in enumerate(meetings, start=1):
                    check_cancel(cancel)
                    if progress is not None:
                        progress(index, total)
                    remaining = archive_import.MAX_BYTES - total_bytes
                    if remaining < 0:
                        raise ValueError("Archive exceeds the 20 GB size limit")
                    item, size, crc = _stage_single_meeting(
                        meeting,
                        directory,
                        remaining,
                        tz=tz,
                        cancel=cancel,
                        is_trashed=is_trashed,
                    )
                    try:
                        if total_bytes + size > archive_import.MAX_BYTES:
                            raise ValueError("Archive exceeds the 20 GB size limit")
                        entry_name = unique_entry_name(meeting.title, seen_names)
                        _write_stored_entry(archive, raw_output, entry_name, item, size, crc, cancel)
                        total_bytes += size
                    finally:
                        item.unlink(missing_ok=True)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        check_cancel(cancel)
        with stage.open("rb") as verify_stream:
            archive_import.preflight(verify_stream)
        check_cancel(cancel)
        result = _publish(stage, directory, "Cassini", "zip")
        _fsync_directory(directory)
        return result
    except InterruptedError:
        check_cancel(cancel)
        raise
    finally:
        if stage:
            stage.unlink(missing_ok=True)
