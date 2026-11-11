# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded ZIP preflight and streaming; archive paths never become disk paths."""

import os
import re
import stat
import struct
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from .export_audio import check_cancel

MAX_ENTRIES = 2_000
MAX_BYTES = 20_000_000_000
MAX_RATIO = 100
MAX_DIRECTORY = 2 * 1024**2
READ_BLOCK = 64 * 1024
BLOCK = 256 * 1024
ENCRYPTED_FLAGS = 0x2041
AES_METHOD = 99


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
    if size < 22:
        raise ArchiveError("damaged")
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
        raise ArchiveError("damaged")
    end_offset = size - len(tail) + at
    cd_limit = end_offset
    if count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        if end_offset < 20:
            raise ArchiveError("damaged")
        stream.seek(end_offset - 20)
        locator = stream.read(20)
        magic, disk, position, disks = struct.unpack("<4sLQL", locator)
        if (
            magic != b"PK\x06\x07"
            or disk
            or disks != 1
            or position > end_offset - 20
            or end_offset - 20 - position < 56
        ):
            raise ArchiveError("damaged")
        stream.seek(position)
        record = stream.read(56)
        magic, record_size, _made, _need, disk, directory_disk, on_disk, count, length, offset = struct.unpack(
            "<4sQ2H2L4Q", record
        )
        if magic != b"PK\x06\x06" or record_size < 44 or position + 12 + record_size != end_offset - 20:
            raise ArchiveError("damaged")
        if disk or directory_disk or on_disk != count:
            raise ArchiveError("damaged")
        cd_limit = position
    if count > MAX_ENTRIES or length > MAX_DIRECTORY:
        raise ArchiveError("limit")
    if offset > cd_limit or length > cd_limit - offset:
        raise ArchiveError("damaged")
    stream.seek(0)
    return offset


def _local_payload_offset(stream, entry, expected_name, cd_offset):
    if entry.header_offset < 0 or entry.header_offset + 30 > cd_offset:
        raise ArchiveError("damaged")
    stream.seek(entry.header_offset)
    local_header = stream.read(30)
    if len(local_header) != 30:
        raise ArchiveError("damaged")
    magic, _ver, local_flags, local_method, _time, _date, _crc, _csize, _usize, fname_len, extra_len = struct.unpack(
        "<4s5H3L2H", local_header
    )
    if magic != b"PK\x03\x04":
        raise ArchiveError("damaged")
    if (local_flags & ENCRYPTED_FLAGS) or local_method == AES_METHOD:
        raise ArchiveError("encrypted")
    if local_method != entry.compress_type:
        raise ArchiveError("damaged")
    raw_name = stream.read(fname_len)
    if len(raw_name) != fname_len:
        raise ArchiveError("damaged")
    try:
        decoded_name = raw_name.decode("utf-8" if (local_flags & 0x800) else "cp437")
    except UnicodeDecodeError as error:
        raise ArchiveError("damaged") from error
    if decoded_name != expected_name:
        raise ArchiveError("damaged")
    payload_offset = entry.header_offset + 30 + fname_len + extra_len
    if payload_offset + entry.compress_size > cd_offset:
        raise ArchiveError("damaged")
    return payload_offset


def entries(archive, budget, *, cd_offset=None):
    values = archive.infolist()
    if len(values) > budget.entries:
        raise ArchiveError("limit")
    max_ratio = getattr(budget, "ratio", MAX_RATIO)
    stream = archive.fp
    if cd_offset is None and stream is not None:
        cd_offset = stream.seek(0, os.SEEK_END)
    total = 0
    spans = []
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
            or (not entry.is_dir() and path.name in ("", ".", ".."))
        ):
            raise ArchiveError("unsafe")
        if (entry.flag_bits & ENCRYPTED_FLAGS) or entry.compress_type == AES_METHOD:
            raise ArchiveError("encrypted")
        if entry.file_size < 0 or entry.compress_size < 0:
            raise ArchiveError("damaged")
        if entry.file_size > entry.compress_size * max_ratio:
            raise ArchiveError("limit")
        if entry.compress_type == zipfile.ZIP_STORED and entry.file_size != entry.compress_size:
            raise ArchiveError("damaged")
        total += entry.file_size
        if total > budget.bytes:
            raise ArchiveError("limit")
        if stream is not None:
            payload_offset = _local_payload_offset(stream, entry, name, cd_offset)
            spans.append((entry.header_offset, payload_offset + entry.compress_size))
    prev_end = 0
    for start, end in sorted(spans):
        if start < prev_end:
            raise ArchiveError("damaged")
        prev_end = end
    return values


def _safe_target_name(filename):
    base = PurePosixPath(filename).name
    suffix = Path(base).suffix
    stem = base[: len(base) - len(suffix)] if suffix else base
    limit = max(1, 255 - len(suffix.encode("utf-8")))
    trimmed = stem.encode("utf-8")[:limit].decode("utf-8", errors="ignore") or "recording"
    return trimmed + suffix


def extract(archive, entry, folder, cancel=None, *, budget=None):
    """One generated private destination; enforces byte and 100:1 bounds on actual read."""
    check_cancel(cancel)
    target = Path(folder) / _safe_target_name(entry.filename)
    stream = archive.fp
    name = getattr(entry, "orig_filename", entry.filename)
    end_bound = stream.seek(0, os.SEEK_END)
    payload_offset = _local_payload_offset(stream, entry, name, end_bound)
    stream.seek(payload_offset)
    max_bytes = budget.bytes if budget is not None else MAX_BYTES
    max_ratio = getattr(budget, "ratio", MAX_RATIO) if budget is not None else MAX_RATIO
    ratio_limit = entry.compress_size * max_ratio
    written = 0
    crc = 0

    def current_read():
        return budget.read_bytes if budget is not None and hasattr(budget, "read_bytes") else written

    def record_read(count):
        if budget is not None and hasattr(budget, "read_bytes"):
            budget.read_bytes += count

    with target.open("xb") as outgoing:
        os.chmod(target, 0o600)

        def emit(chunk):
            nonlocal written, crc
            if not chunk:
                return
            check_cancel(cancel)
            written += len(chunk)
            record_read(len(chunk))
            if current_read() > max_bytes or written > ratio_limit:
                raise ArchiveError("limit")
            if written <= entry.file_size:
                crc = zlib.crc32(chunk, crc)
                outgoing.write(chunk)

        if entry.compress_type == zipfile.ZIP_STORED:
            remaining = entry.compress_size
            while remaining > 0:
                check_cancel(cancel)
                chunk = stream.read(min(BLOCK, remaining))
                if not chunk:
                    raise zipfile.BadZipFile("Truncated stored entry")
                remaining -= len(chunk)
                emit(chunk)
        elif entry.compress_type == zipfile.ZIP_DEFLATED:
            inflater = zlib.decompressobj(-15)
            remaining = entry.compress_size
            while remaining > 0 or inflater.unconsumed_tail:
                check_cancel(cancel)
                if inflater.unconsumed_tail:
                    raw = inflater.unconsumed_tail
                else:
                    raw = stream.read(min(READ_BLOCK, remaining))
                    if not raw:
                        raise zipfile.BadZipFile("Truncated deflate entry")
                    remaining -= len(raw)
                cap = min(BLOCK, max(1, min(ratio_limit - written, max_bytes - current_read()) + 1))
                emit(inflater.decompress(raw, cap))
                if inflater.eof:
                    if inflater.unused_data or remaining > 0:
                        raise zipfile.BadZipFile("Trailing bytes in deflate entry")
                    break
            emit(inflater.flush())
            if not inflater.eof:
                raise zipfile.BadZipFile("Incomplete deflate entry")
        else:
            raise zipfile.BadZipFile("Unsupported compression method")
    if written != entry.file_size or (crc & 0xFFFFFFFF) != entry.CRC:
        raise zipfile.BadZipFile("Corrupt archive entry")
    check_cancel(cancel)
    return target


def preflight(stream):
    try:
        return _preflight(stream)
    except ArchiveError:
        raise
    except (struct.error, OSError, ValueError, OverflowError) as error:
        raise ArchiveError("damaged") from error
