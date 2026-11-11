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
AES_EXTRA = 0x9901
STRONG_ENCRYPTION_EXTRA = 0x0017
UNICODE_PATH_EXTRA = 0x7075


class ArchiveError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def entry_name(entry):
    return getattr(entry, "orig_filename", entry.filename)


def normalized_entry_path(name):
    return name.replace("\\", "/")


def is_directory(entry):
    norm = normalized_entry_path(entry_name(entry))
    mode = entry.external_attr >> 16
    dos_dir = mode == 0 and bool(entry.external_attr & 0x10) and entry.file_size == 0
    return norm.endswith("/") or stat.S_IFMT(mode) == stat.S_IFDIR or dos_dir


def is_macos_metadata(entry):
    norm = normalized_entry_path(entry_name(entry)).rstrip("/")
    if not norm:
        return False
    path = PurePosixPath(norm)
    return "__MACOSX" in path.parts or path.name.startswith("._")


def _validate_entry_name(name, *, directory=False):
    if not name or "\0" in name:
        raise ArchiveError("unsafe")
    norm = normalized_entry_path(name)
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise ArchiveError("unsafe")
    parts = [part for part in norm.split("/") if part]
    if not parts or ".." in parts:
        raise ArchiveError("unsafe")
    if not directory and norm.split("/")[-1] in ("", ".", ".."):
        raise ArchiveError("unsafe")


def _inspect_extra(extra, raw_name=None, *, directory=False):
    at = 0
    size = len(extra)
    while at < size:
        if at + 4 > size:
            raise ArchiveError("damaged")
        header_id, length = struct.unpack_from("<HH", extra, at)
        if at + 4 + length > size:
            raise ArchiveError("damaged")
        if header_id in (AES_EXTRA, STRONG_ENCRYPTION_EXTRA):
            raise ArchiveError("encrypted")
        if header_id == UNICODE_PATH_EXTRA and raw_name is not None and length >= 5 and extra[at + 4] == 1:
            expected_crc = struct.unpack_from("<L", extra, at + 5)[0]
            if (zlib.crc32(raw_name) & 0xFFFFFFFF) == expected_crc:
                try:
                    unicode_name = extra[at + 9 : at + 4 + length].decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ArchiveError("damaged") from error
                _validate_entry_name(unicode_name, directory=directory)
        at += 4 + length


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
    end_offset = size - len(tail) + at
    cd_limit = end_offset
    locator = b""
    if end_offset >= 20:
        stream.seek(end_offset - 20)
        locator = stream.read(20)
    if len(locator) == 20 and locator[:4] == b"PK\x06\x07":
        if (
            disk not in (0, 0xFFFF)
            or directory_disk not in (0, 0xFFFF)
            or (on_disk != count and 0xFFFF not in (on_disk, count))
        ):
            raise ArchiveError("damaged")
        _, loc_disk, position, disks = struct.unpack("<4sLQL", locator)
        if loc_disk != 0 or disks not in (0, 1) or position > end_offset - 20 or end_offset - 20 - position < 56:
            raise ArchiveError("damaged")
        stream.seek(position)
        record = stream.read(56)
        if len(record) != 56:
            raise ArchiveError("damaged")
        magic, record_size, _made, _need, z_disk, z_dir_disk, z_on_disk, z_count, z_length, z_offset = struct.unpack(
            "<4sQ2H2L4Q", record
        )
        if magic != b"PK\x06\x06" or record_size < 44 or position + 12 + record_size != end_offset - 20:
            raise ArchiveError("damaged")
        if z_disk != 0 or z_dir_disk != 0 or z_on_disk != z_count:
            raise ArchiveError("damaged")
        count, length, offset = z_count, z_length, z_offset
        cd_limit = position
    elif disk != 0 or directory_disk != 0 or on_disk != count or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise ArchiveError("damaged")
    if count > MAX_ENTRIES or length > MAX_DIRECTORY:
        raise ArchiveError("limit")
    if offset > cd_limit or length > cd_limit - offset:
        raise ArchiveError("damaged")
    stream.seek(0)
    return offset


def _names_match(candidate, expected, *, directory=False):
    if candidate == expected:
        return True
    if directory:
        return normalized_entry_path(candidate).rstrip("/") == normalized_entry_path(expected).rstrip("/")
    return False


def _local_payload_offset(stream, entry, expected_name, cd_offset, *, directory=False):
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
    payload_offset = entry.header_offset + 30 + fname_len + extra_len
    if payload_offset + entry.compress_size > cd_offset:
        raise ArchiveError("damaged")
    raw_name = stream.read(fname_len)
    if len(raw_name) != fname_len:
        raise ArchiveError("damaged")
    local_extra = stream.read(extra_len)
    if len(local_extra) != extra_len:
        raise ArchiveError("damaged")
    _inspect_extra(local_extra, raw_name, directory=directory)
    if local_method != entry.compress_type:
        raise ArchiveError("damaged")
    decoded_candidates = []
    for encoding in dict.fromkeys(
        (
            "utf-8" if (local_flags & 0x800) else "cp437",
            "utf-8" if (entry.flag_bits & 0x800) else "cp437",
        )
    ):
        try:
            decoded_candidates.append(raw_name.decode(encoding))
        except UnicodeDecodeError:
            continue
    if not decoded_candidates:
        raise ArchiveError("damaged")
    for candidate in decoded_candidates:
        _validate_entry_name(candidate, directory=directory)
    if not any(_names_match(candidate, expected_name, directory=directory) for candidate in decoded_candidates):
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
        name = entry_name(entry)
        directory = is_directory(entry)
        mode = entry.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ArchiveError("unsafe")
        _validate_entry_name(name, directory=directory)
        if (entry.flag_bits & ENCRYPTED_FLAGS) or entry.compress_type == AES_METHOD:
            raise ArchiveError("encrypted")
        raw_central_name = name.encode("utf-8" if (entry.flag_bits & 0x800) else "cp437", errors="surrogateescape")
        _inspect_extra(entry.extra, raw_central_name, directory=directory)
        if getattr(entry, "volume", 0) != 0:
            raise ArchiveError("damaged")
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
            payload_offset = _local_payload_offset(stream, entry, name, cd_offset, directory=directory)
            spans.append((entry.header_offset, payload_offset + entry.compress_size))
    prev_end = 0
    for start, end in sorted(spans):
        if start < prev_end:
            raise ArchiveError("damaged")
        prev_end = end
    return values


def _safe_target_name(filename):
    base = PurePosixPath(normalized_entry_path(filename)).name
    suffix = Path(base).suffix
    stem = base[: len(base) - len(suffix)] if suffix else base
    limit = max(1, 255 - len(suffix.encode("utf-8")))
    trimmed = stem.encode("utf-8")[:limit].decode("utf-8", errors="ignore") or "recording"
    return trimmed + suffix


def extract(archive, entry, folder, cancel=None, *, budget=None):
    """One generated private destination; enforces byte and 100:1 bounds on actual read."""
    check_cancel(cancel)
    name = entry_name(entry)
    directory = is_directory(entry)
    target = Path(folder) / _safe_target_name(name)
    stream = archive.fp
    end_bound = stream.seek(0, os.SEEK_END)
    payload_offset = _local_payload_offset(stream, entry, name, end_bound, directory=directory)
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
            raw_consumed = 0
            while remaining > 0 or inflater.unconsumed_tail:
                check_cancel(cancel)
                if inflater.unconsumed_tail:
                    raw = inflater.unconsumed_tail
                else:
                    raw = stream.read(min(READ_BLOCK, remaining))
                    if not raw:
                        raise zipfile.BadZipFile("Truncated deflate entry")
                    remaining -= len(raw)
                    raw_consumed += len(raw)
                cap = min(BLOCK, max(1, min(ratio_limit - written, max_bytes - current_read()) + 1))
                emit(inflater.decompress(raw, cap))
                if inflater.eof:
                    actual_compressed = raw_consumed - len(inflater.unused_data) - len(inflater.unconsumed_tail)
                    if written > actual_compressed * max_ratio:
                        raise ArchiveError("limit")
                    if inflater.unused_data or remaining > 0:
                        raise zipfile.BadZipFile("Trailing bytes in deflate entry")
                    break
            emit(inflater.flush())
            actual_compressed = raw_consumed - len(inflater.unused_data) - len(inflater.unconsumed_tail)
            if written > actual_compressed * max_ratio:
                raise ArchiveError("limit")
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
