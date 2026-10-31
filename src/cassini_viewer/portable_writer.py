# SPDX-License-Identifier: GPL-3.0-or-later
"""Cassini descriptors and a streaming OpusTags rewrite; no audio is re-encoded."""

import base64
import gzip
import hashlib
import json
import re
import struct
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone

from . import portable
from .export_audio import check_cancel
from .transcript import normalize_quote_items
from .ux_formats import _local

ENCODING = "base64url+gzip+utf8json"
MANIFEST_MIME = "application/vnd.cassini.portable-meeting+json"
WORDS_MIME = "application/vnd.cassini.transcript-words+json"
SCHEMA = "https://format.gocassini.com/schema/cassini-portable-meeting-manifest-v1.schema.json"
HINT = (
    "Concatenate CASSINI_PAYLOAD_000..N for the manifest; for a transcript body concatenate "
    "CASSINI_TX_<ID>_PAYLOAD_000..N. Each chunk set: base64url decode, gzip decompress, parse UTF-8 JSON."
)


def chunk_set(prefix, value, mime):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > portable.INFLATE_CEILING:
        raise ValueError("Exported metadata exceeds the portable format limit")
    gz = gzip.compress(raw, mtime=0)
    encoded = base64.urlsafe_b64encode(gz).rstrip(b"=").decode()
    chunks = [encoded[i : i + 4096] for i in range(0, len(encoded), 4096)]
    if len(chunks) > portable.MAX_CHUNKS:
        raise ValueError("Exported metadata exceeds the portable chunk limit")
    ref = dict(
        prefix=prefix,
        chunkCount=len(chunks),
        sha256=hashlib.sha256(raw).hexdigest(),
        rawBytes=len(raw),
        gzipBytes=len(gz),
        mime=mime,
        encoding=ENCODING,
    )
    tags = {
        prefix + key: str(value)
        for key, value in dict(
            MIME=mime,
            ENCODING=ENCODING,
            CHUNK_COUNT=len(chunks),
            SHA256=ref["sha256"],
            RAW_BYTES=len(raw),
            GZIP_BYTES=len(gz),
        ).items()
    }
    tags.update({f"{prefix}{i:03d}": chunk for i, chunk in enumerate(chunks)})
    return tags, ref


def recording_date(value):
    """Only an absolute DATE with an explicit, known offset is capture time."""
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value)
        or value.endswith("-00:00")
        or not value.endswith("Z")
        and int(value[-2:]) >= 60
    ):
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return None
    offset = instant.utcoffset().total_seconds() / 60
    return instant.replace(microsecond=0).isoformat() if -720 <= offset <= 840 else None


def descriptors(meeting, digest, *, policy=None, tz=None, recorded_at_local=None, recorded_date=None):
    items = (
        [
            dict(
                speaker=w.speaker,
                startMs=w.start_ms,
                endMs=w.end_ms,
                text=w.text,
                **({"lowConfidenceSpeaker": True} if w.low_confidence else {}),
            )
            for w in sorted(meeting.words, key=lambda w: w.start_ms)
        ]
        if meeting.transcript == "done"
        else []
    )
    body = dict(format=portable.WORDS_FORMAT, wordCount=len(items), items=normalize_quote_items(items))
    words_tags, reference = chunk_set("CASSINI_TX_WORDS_PAYLOAD_", body, WORDS_MIME)
    shape = dict(
        sampleRate=48000,
        channels=digest["channels"],
        sampleCount=digest["sampleCount"],
        durationMs=digest["durationMs"],
    )
    date = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    capture_date = recording_date(recorded_date)
    started = None
    if meeting.origin == "own" and meeting.published_file is None and meeting.started_at_ms is not None:
        offset = meeting.recording_offset_minutes
        if type(offset) is int and -720 <= offset <= 840:
            started = datetime.fromtimestamp(meeting.started_at_ms // 1000, timezone(timedelta(minutes=offset)))
        else:
            started = _local(meeting.started_at_ms // 1000 * 1000, tz)
        capture_date = capture_date or started.isoformat(timespec="seconds")
    identifier = "mtg_" + digest["sha256"]
    manifest = dict(
        kind="cassini-portable-meeting",
        version=1,
        profile="ogg-opus",
        meeting=dict(id=identifier, title=meeting.title, createdAtUtc=date, durationMs=shape["durationMs"]),
        audio=dict(container="ogg", codec="opus", **shape),
        integrity=dict(matchPolicy="exact-opus-audio-v1", opusAudioSha256=digest["sha256"], **shape),
        speakers=[dict(id=s.id, label=s.name) for s in meeting.speakers],
        transcripts=[
            dict(id="words", default=True, format=portable.WORDS_FORMAT, wordCount=len(items), payloadRef=reference)
        ],
    )
    if recorded_at_local is not None:
        # A Received row's Library date may be artifact/export/import time.
        # Carry only an explicitly stored recording wall-clock label forward.
        try:
            parsed = datetime.strptime(recorded_at_local, "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            pass
        else:
            if parsed.strftime("%Y-%m-%dT%H:%M:%S") == recorded_at_local:
                manifest["meeting"]["recordedAtLocal"] = recorded_at_local
    elif started is not None:
        manifest["meeting"]["recordedAtLocal"] = started.strftime("%Y-%m-%dT%H:%M:%S")
    if policy is not None:
        manifest["audioEncode"] = deepcopy(policy)
    extension = deepcopy(meeting.extensions)
    domain = extension.get("gocassini.com")
    domain = domain if isinstance(domain, dict) else {}
    if meeting.origin == "own" and any(t.start_offset_ms is None for t in meeting.tracks):
        recovery = domain.get("recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        recovery["offsetsAssumedZero"] = True
        domain["recovery"] = recovery
    if meeting.notes:
        domain["notes"] = deepcopy(list(meeting.notes))
    else:
        domain.pop("notes", None)
    if domain:
        extension["gocassini.com"] = domain
    else:
        extension.pop("gocassini.com", None)
    if extension:
        manifest["x"] = extension
    return file_tags(manifest, words_tags, date=capture_date or date), manifest


def file_tags(manifest, words_tags, *, date):
    """Every tag of a file with one default `words` transcript, mirroring `manifest`."""
    manifest_tags, _ = chunk_set("CASSINI_PAYLOAD_", manifest, MANIFEST_MIME)
    meeting, audio, integrity = manifest["meeting"], manifest["audio"], manifest["integrity"]
    tags = dict(
        CASSINI_FORMAT=portable.FORMAT_V1,
        CASSINI_PROFILE="ogg-opus",
        CASSINI_PAYLOAD_SCHEMA=SCHEMA,
        CASSINI_TRANSCRIPT_IDS="words",
        CASSINI_TRANSCRIPT_DEFAULT="words",
        CASSINI_DECODE_HINT=HINT,
        CASSINI_MEETING_ID=meeting["id"],
        CASSINI_CREATED_AT=meeting["createdAtUtc"],
        CASSINI_SPEAKER_COUNT=str(len(manifest["speakers"])),
        CASSINI_AUDIO_SAMPLE_RATE="48000",
        CASSINI_AUDIO_CHANNELS=str(audio["channels"]),
        CASSINI_AUDIO_SAMPLE_COUNT=str(audio["sampleCount"]),
        CASSINI_AUDIO_DURATION_MS=str(audio["durationMs"]),
        CASSINI_AUDIO_MATCH_POLICY="exact-opus-audio-v1",
        CASSINI_AUDIO_OPUS_SHA256=integrity["opusAudioSha256"],
        TITLE=meeting["title"],
        DATE=date,
        DESCRIPTION="Cassini portable meeting. " + HINT,
    )
    tags.update(manifest_tags)
    tags.update(words_tags)
    return tags


def _pages(stream):
    stream.seek(0)
    while header := stream.read(27):
        if len(header) != 27 or header[:5] != b"OggS\x00":
            raise ValueError("Invalid Ogg page")
        lacing = stream.read(header[26])
        payload = stream.read(sum(lacing))
        if len(lacing) != header[26] or len(payload) != sum(lacing):
            raise ValueError("Truncated Ogg page")
        yield header, lacing, payload


def _page(serial, sequence, flags, granule, lacing, payload):
    header = b"OggS\x00" + struct.pack("<BQIII", flags, granule, serial, sequence, 0) + bytes([len(lacing)])
    data = bytearray(header + lacing + payload)
    struct.pack_into("<I", data, 22, portable.ogg_crc(data))
    return data


def rewrite_tags(stream, output, tags, cancel=None, *, vendor=b"Cassini for GNOME"):
    """Replace only headers/framing; retain packet bytes and granule positions.

    `tags` is a {name: value} dict, or a list of raw comment fields (bytes) when a
    caller edits an existing comment vector and keeps every other field as it was.
    """
    head, _comments, _vendor = portable.read_header(stream)
    if head[9] not in (1, 2):
        raise ValueError("Portable Opus accepts one or two channels")
    stream.seek(14)
    serial = struct.unpack("<I", stream.read(4))[0]
    output.write(_page(serial, 0, 2, 0, bytes([len(head)]), head))
    if isinstance(tags, dict):
        fields = [f"{key}={value}".encode() for key, value in tags.items()]
    else:
        fields = [bytes(field) for field in tags]
    packet = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(fields))
    packet += b"".join(struct.pack("<I", len(field)) + field for field in fields)
    lacing, offset, sequence = bytes([255]) * (len(packet) // 255) + bytes([len(packet) % 255]), 0, 1
    while lacing:
        check_cancel(cancel)
        portion, lacing = lacing[:255], lacing[255:]
        size = sum(portion)
        output.write(_page(serial, sequence, 0 if sequence == 1 else 1, 0, portion, packet[offset : offset + size]))
        sequence += 1
        offset += size
    # Headers can span pages. If a valid source shares the final header page
    # with audio, retain only its audio lacing/payload and clear header continuation.
    packet_index = 0
    for header, lacing, payload in _pages(stream):
        check_cancel(cancel)
        skip_segments = skip_bytes = 0
        if packet_index < 2:
            for size in lacing:
                if packet_index >= 2:
                    break
                skip_segments += 1
                skip_bytes += size
                if size < 255:
                    packet_index += 1
            if skip_segments == len(lacing):
                continue
        flags = header[5] & ~2
        if skip_segments:
            flags &= ~1
        granule = struct.unpack_from("<Q", header, 6)[0]
        output.write(_page(serial, sequence, flags, granule, lacing[skip_segments:], payload[skip_bytes:]))
        sequence += 1
