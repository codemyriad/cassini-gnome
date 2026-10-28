# SPDX-License-Identifier: GPL-3.0-or-later
"""Cassini descriptors and a streaming OpusTags rewrite; no audio is re-encoded."""

import base64
import gzip
import hashlib
import json
import struct
from copy import deepcopy
from datetime import UTC, datetime

from . import portable
from .export_audio import check_cancel

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


def descriptors(meeting, digest, *, policy=None):
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
    body = dict(format=portable.WORDS_FORMAT, wordCount=len(items), items=items)
    words_tags, reference = chunk_set("CASSINI_TX_WORDS_PAYLOAD_", body, WORDS_MIME)
    shape = dict(
        sampleRate=48000,
        channels=digest["channels"],
        sampleCount=digest["sampleCount"],
        durationMs=digest["durationMs"],
    )
    created = (
        datetime.fromtimestamp(meeting.started_at_ms / 1000, UTC)
        if meeting.started_at_ms is not None
        else datetime.now(UTC)
    )
    date = created.isoformat(timespec="milliseconds").replace("+00:00", "Z")
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
    if policy is not None:
        manifest["audioEncode"] = deepcopy(policy)
    extension = deepcopy(meeting.extensions)
    domain = extension.get("gocassini.com")
    domain = domain if isinstance(domain, dict) else {}
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
    manifest_tags, _ = chunk_set("CASSINI_PAYLOAD_", manifest, MANIFEST_MIME)
    tags = dict(
        CASSINI_FORMAT=portable.FORMAT_V1,
        CASSINI_PROFILE="ogg-opus",
        CASSINI_PAYLOAD_SCHEMA=SCHEMA,
        CASSINI_TRANSCRIPT_IDS="words",
        CASSINI_TRANSCRIPT_DEFAULT="words",
        CASSINI_DECODE_HINT=HINT,
        CASSINI_MEETING_ID=identifier,
        CASSINI_CREATED_AT=date,
        CASSINI_SPEAKER_COUNT=str(len(meeting.speakers)),
        CASSINI_AUDIO_SAMPLE_RATE="48000",
        CASSINI_AUDIO_CHANNELS=str(shape["channels"]),
        CASSINI_AUDIO_SAMPLE_COUNT=str(shape["sampleCount"]),
        CASSINI_AUDIO_DURATION_MS=str(shape["durationMs"]),
        CASSINI_AUDIO_MATCH_POLICY="exact-opus-audio-v1",
        CASSINI_AUDIO_OPUS_SHA256=digest["sha256"],
        TITLE=meeting.title,
        DATE=date,
        DESCRIPTION="Cassini portable meeting. " + HINT,
    )
    tags.update(manifest_tags)
    tags.update(words_tags)
    return tags, manifest


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


def rewrite_tags(stream, output, tags, cancel=None):
    """Replace only headers/framing; retain packet bytes and granule positions."""
    head, _comments, _vendor = portable.read_header(stream)
    if head[9] not in (1, 2):
        raise ValueError("Portable Opus accepts one or two channels")
    stream.seek(14)
    serial = struct.unpack("<I", stream.read(4))[0]
    output.write(_page(serial, 0, 2, 0, bytes([len(head)]), head))
    vendor = b"Cassini for GNOME"
    fields = [f"{key}={value}".encode() for key, value in tags.items()]
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
