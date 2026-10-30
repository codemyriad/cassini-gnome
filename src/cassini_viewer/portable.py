# portable.py
#
# Copyright 2026 Silvio Tomatis
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reader for Cassini portable meetings (org.cassini.portable-meeting/1).

A portable meeting is an Ogg Opus file whose OpusTags header carries a
gzipped JSON manifest and one or more word-timed transcripts. The format is
specified at https://format.gocassini.com/; section names in comments below
refer to its SPEC.md.

This module has no GTK dependency so it can be tested on its own.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, BinaryIO

FORMAT_V1 = "org.cassini.portable-meeting/1"
FORMAT_PREFIX = "org.cassini.portable-meeting/"
ENCODING = "base64url+gzip+utf8json"
MANIFEST_PREFIX = "CASSINI_PAYLOAD_"
WORDS_FORMAT = "cassini.words.v1"

FIRST_READ = 1 << 20
MAX_HEADER = 120 << 20  # SPEC: a consumer MAY reject a header over 120 MB
INFLATE_CEILING = 64 << 20  # SPEC: a consumer MAY reject a chunk set above 64 MiB
MAX_COMMENTS = 1 << 20
MAX_CHUNKS = 1 << 16
MAX_JSON_DEPTH = 64

_DECIMAL = re.compile(r"0|[1-9][0-9]*\Z")
_B64URL = re.compile(r"[A-Za-z0-9_-]*\Z")
_WHITESPACE = str.maketrans("", "", " \t\r\n")


class State(enum.StrEnum):
    """The trust states of SPEC.md "Trust and integrity". Names are normative."""

    PLAIN_AUDIO = "plain-audio"
    UNKNOWN_FORMAT = "unknown-cassini-format"
    INVALID = "invalid-cassini-metadata"
    UNVERIFIED = "unverified"
    STALE_AUDIO = "stale-audio"
    OK = "ok"


class NotOpus(ValueError):
    """The file is not an Ogg Opus stream at all."""


class Truncated(ValueError):
    """The bytes end before the OpusTags packet does."""


class CassiniError(ValueError):
    """Cassini metadata is present and something in it cannot be trusted."""

    def __init__(self, message: str, code: str = "decode-failed", tag: str | None = None):
        super().__init__(message)
        self.code = code
        self.tag = tag


@dataclass(frozen=True, slots=True)
class Speaker:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class Word:
    speaker: str
    start_ms: int
    end_ms: int
    text: str
    low_confidence: bool = False


@dataclass(slots=True)
class TranscriptEntry:
    id: str
    format: str
    language: str | None
    word_count: int | None
    created_at: str | None
    payload_ref: dict[str, Any]
    default: bool = False


@dataclass(slots=True)
class Notice:
    """A warning or error worth showing, with a code from the conformance vocabulary."""

    code: str
    message: str
    tag: str | None = None


@dataclass(slots=True)
class Meeting:
    """Everything read from the header. Audio verification comes later."""

    state: State
    reason: str | None = None
    format_id: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    vendor: str | None = None
    manifest: dict[str, Any] | None = None
    speakers: list[Speaker] = field(default_factory=list)
    transcripts: list[TranscriptEntry] = field(default_factory=list)
    default_transcript_id: str | None = None
    summary_markdown: str | None = None
    warnings: list[Notice] = field(default_factory=list)
    channels: int | None = None

    @property
    def is_readable(self) -> bool:
        return self.state in (State.UNVERIFIED, State.STALE_AUDIO, State.OK)

    @property
    def title(self) -> str | None:
        if self.manifest:
            return self.manifest["meeting"]["title"]
        return self.tags.get("TITLE") or None

    @property
    def duration_ms(self) -> int | None:
        if self.manifest:
            return self.manifest["meeting"]["durationMs"]
        return None

    @property
    def created_at(self) -> str | None:
        if self.manifest:
            return self.manifest["meeting"]["createdAtUtc"]
        return None

    @property
    def recorded_at_local(self) -> str | None:
        if self.manifest:
            value = self.manifest["meeting"].get("recordedAtLocal")
            return value if isinstance(value, str) else None
        return None

    def speaker_label(self, speaker_id: str) -> str | None:
        for speaker in self.speakers:
            if speaker.id == speaker_id:
                return speaker.label
        return None


@dataclass(frozen=True, slots=True)
class AudioCheck:
    """The outcome of exact-opus-audio-v1 against the manifest's integrity block."""

    state: State
    reason: str | None = None
    sha256: str | None = None
    sample_count: int | None = None
    duration_ms: int | None = None
    channels: int | None = None


# Reading the comment vector


def _is_load_bearing(name: str) -> bool:
    return name == "CASSINI_FORMAT" or name.startswith(("CASSINI_PAYLOAD_", "CASSINI_TX_"))


def parse_header(data: bytes) -> tuple[bytes, list[tuple[str, str]], str]:
    """Return (OpusHead, [(NAME, value)], vendor) from the front of an Ogg file.

    Names are ASCII upper-cased. Raises Truncated when *data* ends before the
    second packet does, which is the signal to read more.
    """
    packets: list[bytes] = []
    partial = bytearray()
    pos = 0
    while len(packets) < 2:
        if pos + 27 > len(data):
            raise Truncated("file ends inside the Opus header")
        if data[pos : pos + 4] != b"OggS":
            raise NotOpus(f"not an Ogg stream at byte {pos}")
        nsegs = data[pos + 26]
        lacing = data[pos + 27 : pos + 27 + nsegs]
        if len(lacing) < nsegs:
            raise Truncated("file ends inside the Opus header")
        cur = pos + 27 + nsegs
        pos = cur + sum(lacing)
        if pos > len(data):
            raise Truncated("file ends inside the Opus header")
        for n in lacing:
            partial += data[cur : cur + n]
            cur += n
            if n < 255:
                packets.append(bytes(partial))
                partial.clear()
                if len(packets) == 2:
                    break
        if len(partial) > MAX_HEADER:
            raise CassiniError("the comment header is larger than 120 MB")
    head, tags_packet = packets
    if head[:8] != b"OpusHead" or tags_packet[:8] != b"OpusTags":
        raise NotOpus("not an Ogg Opus stream")
    try:
        (vendor_len,) = struct.unpack_from("<I", tags_packet, 8)
        off = 12 + vendor_len
        vendor = tags_packet[12:off].decode("utf-8", "replace")
        (count,) = struct.unpack_from("<I", tags_packet, off)
        off += 4
        if count > MAX_COMMENTS:
            raise CassiniError(f"the comment header declares {count} comments")
        comments = []
        for _ in range(count):
            (size,) = struct.unpack_from("<I", tags_packet, off)
            off += 4
            raw = tags_packet[off : off + size]
            if len(raw) != size:
                raise struct.error("comment runs past the end of the header")
            off += size
            text = raw.decode("utf-8", "replace")
            name, sep, value = text.partition("=")
            if not sep:
                continue
            comments.append((name.upper(), value))
    except struct.error as exc:
        raise NotOpus(f"malformed OpusTags header: {exc}") from None
    return head, comments, vendor


def read_header(stream: BinaryIO) -> tuple[bytes, list[tuple[str, str]], str]:
    """Read only as much of *stream* as the header needs, growing the read."""
    size = FIRST_READ
    while True:
        stream.seek(0)
        data = stream.read(size)
        try:
            return parse_header(data)
        except Truncated:
            if len(data) < size or size >= MAX_HEADER:
                raise
            size *= 2


# Chunk sets


def parse_decimal(value: str | None, tag: str) -> int:
    if value is None:
        raise CassiniError(f"{tag} is missing", "missing-tag", tag)
    if not _DECIMAL.match(value) or len(value) > 16 or int(value) > 2**53 - 1:
        raise CassiniError(f"{tag} is not a decimal number: {value!r}", "malformed-tag", tag)
    return int(value)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CassiniError(f"JSON object repeats the member {key!r}", "json-invalid")
        out[key] = value
    return out


def _depth(value: Any, limit: int = MAX_JSON_DEPTH) -> None:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            raise CassiniError("JSON is nested too deeply", "json-invalid")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


def decode_chunk_set(
    tags: dict[str, str],
    prefix: str,
    count: int,
    *,
    sha256: str | None,
    raw_bytes: int | None,
    gzip_bytes: int | None = None,
    encoding: str | None = ENCODING,
) -> Any:
    """Concatenate PREFIX000..N-1, then base64url, gunzip, check, parse JSON."""
    if encoding != ENCODING:
        raise CassiniError(f"{prefix}*: unsupported encoding {encoding!r}", "unsupported-encoding")
    if count < 1 or count > MAX_CHUNKS:
        raise CassiniError(f"{prefix}*: chunk count {count} is out of range", "malformed-tag")
    if not sha256:
        raise CassiniError(f"{prefix}*: no SHA-256 is declared", "missing-tag", prefix + "SHA256")
    parts = []
    for i in range(count):
        name = f"{prefix}{i:03d}"
        value = tags.get(name)
        if not value:
            raise CassiniError(f"{name} is missing", "chunk-missing", name)
        parts.append(value)
    text = "".join(parts).translate(_WHITESPACE)
    stripped = text.rstrip("=")
    padding = len(text) - len(stripped)
    if padding > 2 or not _B64URL.match(stripped) or len(stripped) % 4 == 1:
        raise CassiniError(f"{prefix}*: not valid base64url", "base64-invalid")
    try:
        gz = base64.b64decode(stripped + "=" * (-len(stripped) % 4), altchars=b"-_", validate=True)
    except binascii.Error as exc:
        raise CassiniError(f"{prefix}*: not valid base64url: {exc}", "base64-invalid") from None
    # RFC 4648 §3.5: non-zero trailing bits are a decode failure.
    if base64.urlsafe_b64encode(gz).decode().rstrip("=") != stripped:
        raise CassiniError(f"{prefix}*: base64url has non-zero trailing bits", "base64-invalid")
    if gzip_bytes is not None and len(gz) != gzip_bytes:
        raise CassiniError(f"{prefix}*: {len(gz)} compressed bytes, {gzip_bytes} declared", "payload-bytes-mismatch")
    limit = min(raw_bytes if raw_bytes is not None else INFLATE_CEILING, INFLATE_CEILING)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        body = inflater.decompress(gz, limit + 1)
    except zlib.error as exc:
        raise CassiniError(f"{prefix}*: gzip will not decode: {exc}", "decode-failed") from None
    if len(body) > limit:
        raise CassiniError(f"{prefix}*: inflates past {limit} bytes", "payload-bytes-mismatch")
    if not inflater.eof or inflater.unused_data:
        raise CassiniError(f"{prefix}*: not exactly one gzip member", "decode-failed")
    if raw_bytes is not None and len(body) != raw_bytes:
        raise CassiniError(f"{prefix}*: {len(body)} bytes, {raw_bytes} declared", "payload-bytes-mismatch")
    if hashlib.sha256(body).hexdigest() != sha256.lower():
        raise CassiniError(f"{prefix}*: SHA-256 does not match", "payload-sha256-mismatch", prefix + "SHA256")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except UnicodeDecodeError:
        raise CassiniError(f"{prefix}*: payload is not UTF-8", "json-invalid") from None
    except (json.JSONDecodeError, RecursionError) as exc:
        raise CassiniError(f"{prefix}*: payload is not JSON: {exc}", "json-invalid") from None
    _depth(value)
    return value


# The manifest


def _require(obj: Any, key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise CassiniError(f"manifest {where}.{key} is missing", "manifest-invalid")
    value = obj[key]
    if isinstance(value, bool) and kind is not bool:
        raise CassiniError(f"manifest {where}.{key} has the wrong type", "manifest-invalid")
    if not isinstance(value, kind):
        raise CassiniError(f"manifest {where}.{key} has the wrong type", "manifest-invalid")
    return value


def _transcript_entry(raw: Any) -> TranscriptEntry | None:
    """A transcripts[] entry, or None when it is unusable as a whole."""
    if not isinstance(raw, dict):
        return None
    tid, fmt, ref = raw.get("id"), raw.get("format"), raw.get("payloadRef")
    if not isinstance(tid, str) or not tid or not isinstance(fmt, str) or not isinstance(ref, dict):
        return None
    word_count = raw.get("wordCount")
    return TranscriptEntry(
        id=tid,
        format=fmt,
        language=raw.get("language") if isinstance(raw.get("language"), str) else None,
        word_count=word_count if isinstance(word_count, int) and not isinstance(word_count, bool) else None,
        created_at=raw.get("createdAtUtc") if isinstance(raw.get("createdAtUtc"), str) else None,
        payload_ref=ref,
        default=raw.get("default") is True,
    )


def _check_manifest(manifest: Any, format_tag: str) -> None:
    if not isinstance(manifest, dict):
        raise CassiniError("the manifest is not a JSON object", "manifest-invalid")
    if manifest.get("kind") != "cassini-portable-meeting":
        raise CassiniError("manifest kind is not cassini-portable-meeting", "manifest-invalid")
    if manifest.get("profile") != "ogg-opus":
        raise CassiniError("manifest profile is not ogg-opus", "manifest-invalid")
    version = manifest.get("version")
    if isinstance(version, bool) or version != 1 or format_tag.lower() != FORMAT_V1:
        raise CassiniError("manifest version disagrees with CASSINI_FORMAT", "manifest-invalid")
    meeting = _require(manifest, "meeting", dict, "")
    for key in ("id", "title", "createdAtUtc"):
        if not _require(meeting, key, str, "meeting"):
            raise CassiniError(f"manifest meeting.{key} is empty", "manifest-invalid")
    _require(meeting, "durationMs", int, "meeting")
    audio = _require(manifest, "audio", dict, "")
    for key in ("sampleRate", "channels", "sampleCount", "durationMs"):
        _require(audio, key, int, "audio")
    for key in ("container", "codec"):
        _require(audio, key, str, "audio")
    integrity = _require(manifest, "integrity", dict, "")
    _require(integrity, "matchPolicy", str, "integrity")
    _require(integrity, "opusAudioSha256", str, "integrity")
    for key in ("sampleRate", "channels", "sampleCount", "durationMs"):
        _require(integrity, key, int, "integrity")
    speakers = _require(manifest, "speakers", list, "")
    for speaker in speakers:
        if not isinstance(speaker, dict) or not isinstance(speaker.get("id"), str) or not speaker["id"]:
            raise CassiniError("a speakers[] entry has no id", "manifest-invalid")
        if not isinstance(speaker.get("label"), str):
            raise CassiniError("a speakers[] entry has no label", "manifest-invalid")
    transcripts = _require(manifest, "transcripts", list, "")
    if not transcripts:
        raise CassiniError("manifest transcripts[] is empty", "manifest-invalid")


def _summary_markdown(manifest: dict[str, Any]) -> str | None:
    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        name = attachment.get("name")
        if not isinstance(name, str) or name.strip().lower() != "summary.md":
            continue
        try:
            return base64.b64decode(attachment.get("contentBase64", ""), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, TypeError, ValueError):
            return None
    return None


def interpret(head: bytes, comments: list[tuple[str, str]], vendor: str | None = None) -> Meeting:
    """Classify a parsed header and, when possible, decode its manifest."""
    tags: dict[str, str] = {}
    duplicates: list[str] = []
    for name, value in comments:
        if name in tags:
            duplicates.append(name)
            continue
        tags[name] = value
    channels = head[9] if len(head) > 9 else None
    meeting = Meeting(state=State.PLAIN_AUDIO, tags=tags, vendor=vendor, channels=channels)

    format_tag = tags.get("CASSINI_FORMAT")
    if format_tag is None:
        return meeting
    meeting.format_id = format_tag
    if not format_tag.lower().startswith(FORMAT_PREFIX) or format_tag.lower() != FORMAT_V1:
        meeting.state = State.UNKNOWN_FORMAT
        meeting.reason = f"This file uses {format_tag}, a version this app cannot read."
        return meeting

    for name in duplicates:
        if _is_load_bearing(name):
            meeting.state = State.INVALID
            meeting.reason = f"{name} appears more than once"
            meeting.warnings.append(Notice("duplicate-tag", meeting.reason, name))
            return meeting
        meeting.warnings.append(Notice("duplicate-tag", f"{name} appears more than once", name))

    try:
        # SPEC "Cassini descriptor tags" / "Which declaration wins": these
        # manifest tags are the record, not optional copies of manifest fields.
        for suffix in ("ENCODING", "CHUNK_COUNT", "SHA256", "RAW_BYTES", "GZIP_BYTES", "MIME", "SCHEMA"):
            tag = MANIFEST_PREFIX + suffix
            if not tags.get(tag):
                raise CassiniError(f"{tag} is missing or empty", "payload-descriptor-incomplete", tag)
        encoding = tags.get("CASSINI_PAYLOAD_ENCODING")
        count = parse_decimal(tags.get("CASSINI_PAYLOAD_CHUNK_COUNT"), "CASSINI_PAYLOAD_CHUNK_COUNT")
        raw_bytes = parse_decimal(tags.get("CASSINI_PAYLOAD_RAW_BYTES"), "CASSINI_PAYLOAD_RAW_BYTES")
        gzip_bytes = parse_decimal(tags.get("CASSINI_PAYLOAD_GZIP_BYTES"), "CASSINI_PAYLOAD_GZIP_BYTES")
        manifest = decode_chunk_set(
            tags,
            MANIFEST_PREFIX,
            count,
            sha256=tags.get("CASSINI_PAYLOAD_SHA256"),
            raw_bytes=raw_bytes,
            gzip_bytes=gzip_bytes,
            encoding=encoding,
        )
        _check_manifest(manifest, format_tag)
    except CassiniError as exc:
        meeting.state = State.INVALID
        meeting.reason = str(exc)
        meeting.warnings.append(Notice(exc.code, str(exc), exc.tag))
        return meeting

    meeting.state = State.UNVERIFIED
    meeting.reason = "The audio has not been checked yet."
    meeting.manifest = manifest
    meeting.speakers = [Speaker(s["id"], s["label"]) for s in manifest["speakers"]]
    meeting.transcripts = [e for e in map(_transcript_entry, manifest["transcripts"]) if e]
    meeting.default_transcript_id = resolve_default(meeting.transcripts)
    meeting.summary_markdown = _summary_markdown(manifest)
    _compare_copies(meeting)
    return meeting


def resolve_default(entries: list[TranscriptEntry]) -> str | None:
    """SPEC "Resolving a transcript": first flagged default, else the first entry."""
    for entry in entries:
        if entry.default:
            return entry.id
    return entries[0].id if entries else None


def _compare_copies(meeting: Meeting) -> None:
    """Note where tags disagree with the manifest. The manifest wins."""
    tags, manifest = meeting.tags, meeting.manifest
    assert manifest is not None
    ids_tag = tags.get("CASSINI_TRANSCRIPT_IDS")
    if ids_tag is not None:
        tagged = {i for i in ids_tag.split(",") if i}
        if tagged != {e.id for e in meeting.transcripts}:
            meeting.warnings.append(
                Notice(
                    "tag-manifest-disagreement",
                    "CASSINI_TRANSCRIPT_IDS disagrees with the manifest",
                    "CASSINI_TRANSCRIPT_IDS",
                )
            )
    default_tag = tags.get("CASSINI_TRANSCRIPT_DEFAULT")
    if default_tag and default_tag != meeting.default_transcript_id:
        meeting.warnings.append(
            Notice(
                "tag-manifest-disagreement",
                "CASSINI_TRANSCRIPT_DEFAULT disagrees with the manifest",
                "CASSINI_TRANSCRIPT_DEFAULT",
            )
        )
    for entry in meeting.transcripts:
        ref = entry.payload_ref
        prefix = ref.get("prefix")
        if not isinstance(prefix, str):
            continue
        for member, suffix in (("chunkCount", "CHUNK_COUNT"), ("sha256", "SHA256"), ("rawBytes", "RAW_BYTES")):
            tag = prefix + suffix
            if tag in tags and str(ref.get(member)) != tags[tag]:
                meeting.warnings.append(Notice("tag-manifest-disagreement", f"{tag} disagrees with the manifest", tag))


def read_meeting(path: str) -> Meeting:
    """Read and classify the header of the file at *path*. Never reads the audio."""
    with open(path, "rb") as stream:
        head, comments, vendor = read_header(stream)
    return interpret(head, comments, vendor)


# Transcript bodies


def load_words(meeting: Meeting, transcript_id: str) -> list[Word]:
    """Decode one word-timed transcript. Raises CassiniError if it is unavailable.

    Items stay in file order: speaker-turn order, never sorted by time.
    """
    entry = next((e for e in meeting.transcripts if e.id == transcript_id), None)
    if entry is None:
        raise CassiniError(f"there is no transcript {transcript_id!r}")
    if entry.format != WORDS_FORMAT:
        raise CassiniError(f"transcript format {entry.format!r} is not supported", "unsupported-format")
    ref = entry.payload_ref
    allowed = {"prefix", "chunkCount", "sha256", "rawBytes", "gzipBytes", "mime", "encoding"}
    if set(ref) - allowed:
        raise CassiniError("payloadRef carries a member this app does not know", "unsupported-format")
    prefix, count = ref.get("prefix"), ref.get("chunkCount")
    if not isinstance(prefix, str) or not isinstance(count, int) or isinstance(count, bool):
        raise CassiniError("payloadRef is incomplete", "manifest-invalid")
    raw_bytes = ref.get("rawBytes") if isinstance(ref.get("rawBytes"), int) else None
    gzip_bytes = ref.get("gzipBytes") if isinstance(ref.get("gzipBytes"), int) else None
    body = decode_chunk_set(
        meeting.tags,
        prefix,
        count,
        sha256=ref.get("sha256") if isinstance(ref.get("sha256"), str) else None,
        raw_bytes=raw_bytes,
        gzip_bytes=gzip_bytes,
        encoding=ref.get("encoding"),
    )
    if not isinstance(body, dict):
        raise CassiniError("transcript body is not a JSON object", "json-invalid")
    items = body.get("items")
    if items is None:
        return []
    if not isinstance(items, list):
        raise CassiniError("transcript items is not an array", "json-invalid")
    words: list[Word] = []
    for item in items:
        if not isinstance(item, dict):
            raise CassiniError("a transcript item is not an object", "json-invalid")
        speaker, start, end, text = item.get("speaker"), item.get("startMs"), item.get("endMs"), item.get("text")
        if not isinstance(speaker, str) or not speaker or not isinstance(text, str):
            raise CassiniError("a transcript item has no speaker or text", "json-invalid")
        if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool):
            raise CassiniError("a transcript item has no timing", "json-invalid")
        if item.get("echo") is not True:
            words.append(Word(speaker, max(0, start), max(start, end), text, item.get("lowConfidenceSpeaker") is True))
    return words


# Audio integrity: exact-opus-audio-v1

_BITREV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def ogg_crc(data: bytes) -> int:
    """The Ogg page CRC: poly 0x04c11db7, init 0, unreflected, no final XOR.

    zlib only implements the reflected variant, but reflecting the input bytes
    and the output word turns one into the other, at C speed.
    """
    raw = zlib.crc32(data.translate(_BITREV), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return int(f"{raw:032b}"[::-1], 2)


def packet_samples(packet: bytes) -> int:
    """Duration of one Opus packet in 48 kHz samples, from its TOC byte (RFC 6716 §3.1)."""
    if not packet:
        raise ValueError("empty Opus packet")
    toc = packet[0]
    config = toc >> 3
    if config >= 16:  # CELT: 2.5, 5, 10, 20 ms
        per_frame = (120, 240, 480, 960)[config & 3]
    elif config >= 12:  # Hybrid: 10, 20 ms
        per_frame = (480, 960)[config & 1]
    else:  # SILK: 10, 20, 40, 60 ms
        per_frame = (480, 960, 1920, 2880)[config & 3]
    code = toc & 3
    if code == 3:
        if len(packet) < 2:
            raise ValueError("code-3 Opus packet has no frame count")
        frames = packet[1] & 0x3F
        if frames == 0:
            raise ValueError("code-3 Opus packet declares zero frames")
    else:
        frames = 1 if code == 0 else 2
    total = per_frame * frames
    if total > 5760:
        raise ValueError("Opus packet is longer than 120 ms")
    return total


def compute_audio_digest(stream: BinaryIO, cancelled=lambda: False) -> dict[str, Any]:
    """Compute exact-opus-audio-v1 over the whole stream. Raises ValueError on rejection."""
    digest = hashlib.sha256(b"org.cassini.opus-packets/1\x00")
    partial = bytearray()
    index = audio_packets = decoded = 0
    serial = pre_skip = channels = final_granule = None
    expect_seq = 0
    stream.seek(0)
    while True:
        if cancelled():
            raise InterruptedError("cancelled")
        header = stream.read(27)
        if not header:
            break
        if len(header) < 27 or header[:4] != b"OggS" or header[4] != 0:
            raise ValueError("bad Ogg page header")
        flags, granule, page_serial, seq, crc = struct.unpack_from("<BQIII", header, 5)
        nsegs = header[26]
        lacing = stream.read(nsegs)
        payload = stream.read(sum(lacing))
        if len(lacing) != nsegs or len(payload) != sum(lacing):
            raise ValueError("truncated Ogg page")
        page = bytearray(header)
        page[22:26] = b"\0\0\0\0"
        if ogg_crc(bytes(page) + lacing + payload) != crc:
            raise ValueError(f"Ogg CRC mismatch on page {seq}")
        if serial is None:
            serial = page_serial
            if not flags & 0x02:
                raise ValueError("first page is not marked beginning-of-stream")
        elif page_serial != serial:
            raise ValueError("multiplexed or chained Ogg stream")
        if seq != expect_seq:
            raise ValueError(f"page sequence gap at page {seq}")
        if bool(flags & 0x01) != bool(partial):
            raise ValueError(f"invalid continuation flag on page {seq}")
        expect_seq += 1
        cur = 0
        for n in lacing:
            partial += payload[cur : cur + n]
            cur += n
            if n == 255:
                continue
            packet = bytes(partial)
            partial.clear()
            if index == 0:
                if len(packet) < 19 or packet[:8] != b"OpusHead" or packet[8] > 15:
                    raise ValueError("malformed OpusHead")
                channels, pre_skip = packet[9], struct.unpack_from("<H", packet, 10)[0]
                canon = bytearray(packet)
                canon[12:16] = b"\0\0\0\0"
                digest.update(b"H" + struct.pack("<Q", len(canon)) + bytes(canon))
            elif index == 1:
                if packet[:8] != b"OpusTags":
                    raise ValueError("second packet is not OpusTags")
            else:
                decoded += packet_samples(packet)
                digest.update(b"A" + struct.pack("<Q", len(packet)) + packet)
                audio_packets += 1
            index += 1
        if flags & 0x04:
            final_granule = granule
    if partial:
        raise ValueError("stream ends inside a packet")
    if final_granule is None or pre_skip is None:
        raise ValueError("missing end-of-stream page")
    if final_granule > 0x7FFFFFFFFFFFFFFF or final_granule < pre_skip:
        raise ValueError("invalid final granule position")
    if decoded < pre_skip:
        raise ValueError("fewer decoded samples than pre-skip")
    sample_count = min(decoded - pre_skip, final_granule - pre_skip)
    digest.update(b"E" + struct.pack("<QQ", audio_packets, sample_count))
    return {
        "sha256": digest.hexdigest(),
        "channels": channels,
        "sampleCount": sample_count,
        "durationMs": sample_count * 1000 // 48000,
    }


def verify_audio(path: str, meeting: Meeting, cancelled=lambda: False) -> AudioCheck:
    """Check the audio against the manifest. Returns the file's final state."""
    if not meeting.is_readable or meeting.manifest is None:
        return AudioCheck(meeting.state, meeting.reason)
    integrity = meeting.manifest["integrity"]
    claimed = integrity["opusAudioSha256"].lower()
    tagged = meeting.tags.get("CASSINI_AUDIO_OPUS_SHA256")
    if set(integrity) - {"matchPolicy", "opusAudioSha256", "sampleRate", "channels", "sampleCount", "durationMs"}:
        return AudioCheck(State.UNVERIFIED, "The integrity record has members this app does not understand.")
    if integrity["matchPolicy"] != "exact-opus-audio-v1":
        return AudioCheck(State.UNVERIFIED, "The integrity record uses an unknown match policy.")
    if tagged is not None and tagged.lower() != claimed:
        return AudioCheck(State.UNVERIFIED, "The audio digest in the tags disagrees with the manifest.")
    try:
        with open(path, "rb") as stream:
            result = compute_audio_digest(stream, cancelled)
    except InterruptedError:
        raise
    except (OSError, ValueError, struct.error) as exc:
        return AudioCheck(State.UNVERIFIED, f"The audio could not be checked: {exc}.")
    mismatches = []
    if result["sha256"] != claimed:
        mismatches.append("digest")
    for key in ("channels", "sampleCount", "durationMs"):
        if result[key] != integrity[key]:
            mismatches.append(key)
    check = AudioCheck(
        State.STALE_AUDIO if mismatches else State.OK,
        f"Mismatch in {', '.join(mismatches)}." if mismatches else None,
        result["sha256"],
        result["sampleCount"],
        result["durationMs"],
        result["channels"],
    )
    return check
