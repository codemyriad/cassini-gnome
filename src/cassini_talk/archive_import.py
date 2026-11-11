# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded ZIP preflight and streaming; archive paths never become disk paths."""

import os
import re
import stat
import struct
from pathlib import Path, PurePosixPath

from .export_audio import check_cancel

MAX_ENTRIES = 1024
MAX_BYTES = 8 * 1024**3
MAX_DIRECTORY = 2 * 1024**2
BLOCK = 1024**2


class ArchiveError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _preflight(stream):
    """Bound ZipFile's central-directory allocation before it parses entries.

    Check both classic and ZIP64 end records. Declared count alone cannot bound
    allocation: ZipFile iterates central-directory bytes, not that count.
    """
    size = stream.seek(0, os.SEEK_END)
    if size > MAX_BYTES + MAX_DIRECTORY + 65557:
        raise ArchiveError("limit")
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    # ZipFile uses the last signature. Never validate a different earlier
    # record when a comment contains an ambiguous/fake trailing signature.
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or at + 22 > len(tail):
        raise ArchiveError("damaged")
    fields = struct.unpack_from("<4s4H2LH", tail, at)
    if at + 22 + fields[-1] != len(tail):
        raise ArchiveError("damaged")
    _, disk, directory_disk, on_disk, count, length, offset, _ = fields
    if disk or directory_disk or on_disk != count:
        raise ArchiveError("unsupported")
    end_offset = size - len(tail) + at
    if count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        if end_offset < 20:
            raise ArchiveError("damaged")
        stream.seek(end_offset - 20)
        locator = stream.read(20)
        magic, disk, position, disks = struct.unpack("<4sLQL", locator)
        if magic != b"PK\x06\x07" or disk or disks != 1 or position > size - 56:
            raise ArchiveError("unsupported")
        stream.seek(position)
        record = stream.read(56)
        magic, record_size, _made, _need, disk, directory_disk, on_disk, count, length, offset = struct.unpack(
            "<4sQ2H2L4Q", record
        )
        if magic != b"PK\x06\x06" or record_size != 44 or position + 56 != end_offset - 20:
            raise ArchiveError("damaged")
        if disk or directory_disk or on_disk != count:
            raise ArchiveError("unsupported")
    if count > MAX_ENTRIES or length > MAX_DIRECTORY:
        raise ArchiveError("limit")
    if offset > size or offset + length > end_offset:
        raise ArchiveError("damaged")
    stream.seek(0)


def entries(archive, budget):
    values = archive.infolist()
    if len(values) > budget.entries:
        raise ArchiveError("limit")
    total = 0
    for entry in values:
        name = getattr(entry, "orig_filename", entry.filename)
        path = PurePosixPath(name)
        mode = entry.external_attr >> 16
        if (
            not name
            or "\0" in name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or re.match(r"^[A-Za-z]:", name)
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
        ):
            raise ArchiveError("unsafe")
        if entry.flag_bits & 1:
            raise ArchiveError("encrypted")
        total += entry.file_size
        if total > budget.bytes:
            raise ArchiveError("limit")
    budget.entries -= len(values)
    budget.bytes -= total
    return values


def extract(archive, entry, folder, cancel=None):
    """One generated private destination; no extract/extractall or entry path use."""
    check_cancel(cancel)
    target = Path(folder) / PurePosixPath(entry.filename).name
    length = 0
    with archive.open(entry) as incoming, target.open("xb") as outgoing:
        os.chmod(target, 0o600)
        while length < entry.file_size:
            check_cancel(cancel)
            block = incoming.read(min(BLOCK, entry.file_size - length))
            if not block:
                raise ArchiveError("damaged")
            length += len(block)
            if length > entry.file_size or length > MAX_BYTES:
                raise ArchiveError("limit")
            outgoing.write(block)
    if length != entry.file_size:
        raise ArchiveError("damaged")
    check_cancel(cancel)
    return target


def preflight(stream):
    try:
        _preflight(stream)
    except ArchiveError:
        raise
    except (struct.error, OSError, ValueError, OverflowError) as error:
        raise ArchiveError("damaged") from error
