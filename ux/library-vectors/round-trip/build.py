#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Build the D112 (L-86, L-87) meeting.json -> .opus -> meeting.json round-trip vectors.

Stdlib only, deterministic, no network, no clock, no app code.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent

CRC_TABLE = []
for _b in range(256):
    _v = _b << 24
    for _ in range(8):
        _v = ((_v << 1) ^ (0x04C11DB7 if _v & 0x80000000 else 0)) & 0xFFFFFFFF
    CRC_TABLE.append(_v)

X_TOP_KEYS = {"id", "revision", "notes", "namePrefix", "namePrefixSource", "published"}
X_MEETING_KEYS = {
    "version",
    "title",
    "titleSource",
    "speakers",
    "speakerMerges",
    "transcript",
    "etaMinutes",
    "error",
    "quoteOverride",
    "received",
    "recordingDetails",
    "startedAt",
    "startedAtSource",
}


def ogg_page(packets: list[bytes], number: int, granule: int, flags: int) -> bytes:
    segments: list[int] = []
    for pkt in packets:
        segments.extend([255] * (len(pkt) // 255) + [len(pkt) % 255])
    assert len(segments) <= 255
    header = (
        b"OggS"
        + struct.pack("<BBQIII", 0, flags, granule, 0x43415353, number, 0)
        + bytes([len(segments)])
    )
    data = bytearray(header + bytes(segments) + b"".join(packets))
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ CRC_TABLE[((crc >> 24) ^ byte) & 255]) & 0xFFFFFFFF
    struct.pack_into("<I", data, 22, crc)
    return bytes(data)


def encode_chunk_tags(prefix: str, body: dict) -> dict[str, str]:
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    compressed = compressed[:9] + b"\xff" + compressed[10:]
    encoded = base64.urlsafe_b64encode(compressed).rstrip(b"=").decode("ascii")
    mime = (
        "application/vnd.cassini.portable-meeting+json"
        if prefix == "CASSINI_PAYLOAD_"
        else "application/vnd.cassini.transcript-words+json"
    )
    return {
        f"{prefix}MIME": mime,
        f"{prefix}ENCODING": "base64url+gzip+utf8json",
        f"{prefix}CHUNK_COUNT": "1",
        f"{prefix}SHA256": hashlib.sha256(raw).hexdigest(),
        f"{prefix}RAW_BYTES": str(len(raw)),
        f"{prefix}GZIP_BYTES": str(len(compressed)),
        f"{prefix}000": encoded,
    }


def split_meeting_to_x(meeting_json: dict, *, automatic_title: str | None) -> dict:
    x_meeting = {k: v for k, v in meeting_json.items() if k in X_MEETING_KEYS}
    passthrough = {
        k: v
        for k, v in meeting_json.items()
        if k not in X_TOP_KEYS and k not in X_MEETING_KEYS
    }
    origin = "Received" if isinstance(meeting_json.get("received"), dict) else "Recording"
    x_cm: dict = {
        "schema": 1,
        "sessionId": meeting_json["id"],
        "origin": origin,
        "namePrefix": meeting_json["namePrefix"],
        "revision": meeting_json["revision"],
        "meeting": x_meeting,
        "automaticTitle": automatic_title,
    }
    if "namePrefixSource" in meeting_json:
        x_cm["namePrefixSource"] = meeting_json["namePrefixSource"]
    if passthrough:
        x_cm["passthrough"] = passthrough
    return x_cm


def build_opus(
    *,
    meeting_json: dict,
    words: dict,
    manifest_speakers: list[dict],
    duration_ms: int,
    created_at_utc: str,
    date_tag: str | None,
    recorded_at_local: str | None,
    manifest_started_at: str | None,
    automatic_title: str | None,
) -> bytes:
    samples = duration_ms * 48
    count = math.ceil((samples + 312) / 960)
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 312, 48000, 0, 0)
    silence = bytes.fromhex("f8fffe")

    digest = hashlib.sha256(b"org.cassini.opus-packets/1\0")
    canonical_head = bytearray(head)
    canonical_head[12:16] = bytes(4)
    digest.update(b"H" + struct.pack("<Q", len(head)) + canonical_head)
    for _ in range(count):
        digest.update(b"A" + struct.pack("<Q", len(silence)) + silence)
    digest.update(b"E" + struct.pack("<QQ", count, samples))
    audio_sha = digest.hexdigest()

    displayed_title = meeting_json["title"] or automatic_title or "Untitled meeting"
    word_tags = encode_chunk_tags("CASSINI_TX_MAIN_PAYLOAD_", words)
    word_ref = {
        "prefix": "CASSINI_TX_MAIN_PAYLOAD_",
        "chunkCount": 1,
        "sha256": word_tags["CASSINI_TX_MAIN_PAYLOAD_SHA256"],
        "rawBytes": int(word_tags["CASSINI_TX_MAIN_PAYLOAD_RAW_BYTES"]),
        "gzipBytes": int(word_tags["CASSINI_TX_MAIN_PAYLOAD_GZIP_BYTES"]),
        "mime": "application/vnd.cassini.transcript-words+json",
        "encoding": "base64url+gzip+utf8json",
    }

    manifest_meeting: dict = {
        "id": "mtg_" + audio_sha,
        "title": displayed_title,
        "createdAtUtc": created_at_utc,
        "durationMs": duration_ms,
    }
    if manifest_started_at is not None:
        manifest_meeting["startedAt"] = manifest_started_at
    if recorded_at_local is not None:
        manifest_meeting["recordedAtLocal"] = recorded_at_local

    x_block: dict = {
        "codemyriad.io": split_meeting_to_x(meeting_json, automatic_title=automatic_title),
    }
    if meeting_json.get("notes"):
        x_block["gocassini.com"] = {"notes": meeting_json["notes"]}

    manifest = {
        "kind": "cassini-portable-meeting",
        "version": 1,
        "profile": "ogg-opus",
        "meeting": manifest_meeting,
        "audio": {
            "container": "ogg",
            "codec": "opus",
            "sampleRate": 48000,
            "channels": 1,
            "sampleCount": samples,
            "durationMs": duration_ms,
        },
        "integrity": {
            "matchPolicy": "exact-opus-audio-v1",
            "opusAudioSha256": audio_sha,
            "sampleRate": 48000,
            "channels": 1,
            "sampleCount": samples,
            "durationMs": duration_ms,
        },
        "speakers": manifest_speakers,
        "transcripts": [
            {
                "id": "main",
                "role": "raw-asr",
                "default": True,
                "format": "cassini.words.v1",
                "wordCount": len(words["items"]),
                "payloadRef": word_ref,
            }
        ],
        "x": x_block,
    }

    tags: dict[str, str] = {"TITLE": displayed_title}
    if date_tag is not None:
        tags["DATE"] = date_tag
    tags.update(
        {
            "CASSINI_FORMAT": "org.cassini.portable-meeting/1",
            "CASSINI_PROFILE": "ogg-opus",
            "CASSINI_PAYLOAD_SCHEMA": "https://format.gocassini.com/schema/cassini-portable-meeting-manifest-v1.schema.json",
            "CASSINI_SPEAKER_COUNT": str(len(manifest_speakers)),
            "CASSINI_TRANSCRIPT_IDS": "main",
            "CASSINI_TRANSCRIPT_DEFAULT": "main",
            "CASSINI_AUDIO_SAMPLE_RATE": "48000",
            "CASSINI_AUDIO_CHANNELS": "1",
            "CASSINI_AUDIO_SAMPLE_COUNT": str(samples),
            "CASSINI_AUDIO_DURATION_MS": str(duration_ms),
            "CASSINI_AUDIO_MATCH_POLICY": "exact-opus-audio-v1",
            "CASSINI_AUDIO_OPUS_SHA256": audio_sha,
            "CASSINI_DECODE_HINT": "Concatenate CASSINI_PAYLOAD_000..N for the manifest; for a transcript body concatenate CASSINI_TX_<ID>_PAYLOAD_000..N. Each chunk set: base64url decode, gzip decompress, parse UTF-8 JSON.",
            **encode_chunk_tags("CASSINI_PAYLOAD_", manifest),
            **word_tags,
        }
    )

    comments = [(f"{k}={v}").encode("utf-8") for k, v in sorted(tags.items())]
    vendor = b"Cassini D112 CC0 round-trip vector"
    opus_tags = (
        b"OpusTags"
        + struct.pack("<I", len(vendor))
        + vendor
        + struct.pack("<I", len(comments))
        + b"".join(struct.pack("<I", len(c)) + c for c in comments)
    )

    out = bytearray(ogg_page([head], 0, 0, 2) + ogg_page([opus_tags], 1, 0, 0))
    for start in range(0, count, 255):
        amount = min(255, count - start)
        last = start + amount == count
        granule = samples + 312 if last else (start + amount) * 960
        out += ogg_page([silence] * amount, 2 + start // 255, granule, 4 if last else 0)
    return bytes(out)


def build_all() -> dict[str, bytes]:
    # Vector 1: speakers[].background: true (D112 / L-86)
    bg_meeting = {
        "version": 1,
        "revision": 4,
        "id": "11111111-2222-4333-8444-555555555555",
        "title": "Project sync",
        "titleSource": "user",
        "notes": [],
        "speakers": [
            {"id": "spk-1", "name": "Sam", "provenance": "user", "colourIndex": 0},
            {"id": "spk-2", "name": "Robin", "provenance": "user", "colourIndex": 1},
            {
                "id": "spk-3",
                "name": "Marta",
                "provenance": "user",
                "colourIndex": 2,
                "background": True,
            },
            {
                "id": "spk-4",
                "name": None,
                "provenance": "unnamed",
                "colourIndex": 3,
                "background": True,
            },
        ],
        "transcript": {
            "status": "done",
            "reference": {
                "kind": "sidecar",
                "file": "transcript.json",
                "format": "cassini.words.v1",
            },
            "model": "whistle",
        },
        "etaMinutes": None,
        "error": None,
        "quoteOverride": None,
        "received": None,
        "namePrefix": "2026-11-23--14.36-",
        "namePrefixSource": "recorded",
    }
    bg_words = {
        "format": "cassini.words.v1",
        "language": "en",
        "items": [
            {"speaker": "spk-1", "startMs": 1000, "endMs": 4000, "text": "Let us review the schedule."},
            {"speaker": "spk-2", "startMs": 4500, "endMs": 8000, "text": "The first part lands this week."},
            {"speaker": "spk-3", "startMs": 8500, "endMs": 10500, "text": "Table by the window is free."},
            {"speaker": "spk-4", "startMs": 11000, "endMs": 13000, "text": "Two espressos please."},
            {"speaker": "spk-1", "startMs": 14000, "endMs": 17000, "text": "Great, I will book the room."},
        ],
    }
    bg_manifest_speakers = [
        {"id": "spk-1", "label": "Sam"},
        {"id": "spk-2", "label": "Robin"},
        {"id": "spk-3", "label": "Marta"},
        {"id": "spk-4", "label": "Speaker 4"},
    ]
    bg_opus = build_opus(
        meeting_json=bg_meeting,
        words=bg_words,
        manifest_speakers=bg_manifest_speakers,
        duration_ms=20000,
        created_at_utc="2026-11-23T13:37:00Z",
        date_tag="2026-11-23T14:36:00+01:00",
        recorded_at_local="2026-11-23T14:36:00",
        manifest_started_at="2026-11-23T14:36:00+01:00",
        automatic_title="Project sync",
    )
    bg_expected = {
        "vector": "background-voice",
        "decision": "D112 (L-86)",
        "rebuiltMeetingJson": bg_meeting,
        "manifestSpeakers": bg_manifest_speakers,
        "speakerCountTag": "4",
        "visibleSpeakerIds": ["spk-1", "spk-2"],
        "backgroundSpeakerIds": ["spk-3", "spk-4"],
        "libraryRowNames": ["Sam", "Robin"],
        "foldedRuns": [{"count": 2, "speakerIds": ["spk-3", "spk-4"]}],
        "preservedNameOnBackgroundSpeaker": {"id": "spk-3", "name": "Marta"},
    }

    # Vector 2: startedAt + startedAtSource: "user" (D112 / L-87)
    date_meeting = {
        "version": 1,
        "revision": 2,
        "id": "66666666-7777-4888-8999-aaaaaaaaaaaa",
        "title": "Imported recorder clip",
        "titleSource": "user",
        "notes": [],
        "speakers": [
            {"id": "spk-1", "name": "Sam", "provenance": "user", "colourIndex": 0},
        ],
        "transcript": {
            "status": "done",
            "reference": {
                "kind": "embedded",
                "file": "recorder-clip.opus",
                "transcriptId": "main",
            },
            "model": "whistle",
        },
        "etaMinutes": None,
        "error": None,
        "quoteOverride": None,
        "received": {
            "originalName": "recorder-clip.opus",
            "originalFolderName": "Downloads",
            "sha256": "0" * 64,
            "importedAtMs": 1795508100000,
        },
        "namePrefix": "2026-11-24--09.15-",
        "namePrefixSource": "import-time",
        "startedAt": "2026-11-23T14:36:00+01:00",
        "startedAtSource": "user",
    }
    date_words = {
        "format": "cassini.words.v1",
        "language": "en",
        "items": [
            {"speaker": "spk-1", "startMs": 500, "endMs": 3500, "text": "Notes from the field recorder."},
        ],
    }
    date_manifest_speakers = [{"id": "spk-1", "label": "Sam"}]
    date_opus = build_opus(
        meeting_json=date_meeting,
        words=date_words,
        manifest_speakers=date_manifest_speakers,
        duration_ms=10000,
        created_at_utc="2026-11-24T08:15:00Z",
        date_tag="2026-11-23T14:36:00+01:00",
        recorded_at_local="2026-11-23T14:36:00",
        manifest_started_at="2026-11-23T14:36:00+01:00",
        automatic_title="recorder-clip",
    )
    date_expected = {
        "vector": "user-started-at",
        "decision": "D112 (L-87)",
        "rebuiltMeetingJson": date_meeting,
        "dateTag": "2026-11-23T14:36:00+01:00",
        "manifestStartedAt": "2026-11-23T14:36:00+01:00",
        "manifestRecordedAtLocal": "2026-11-23T14:36:00",
        "manifestCreatedAtUtc": "2026-11-24T08:15:00Z",
        "namePrefixUnchanged": "2026-11-24--09.15-",
        "namePrefixSourceUnchanged": "import-time",
        "fileRenamed": False,
        "inDateUnknownGroup": False,
    }

    # Vector 3: legacy pre-D112 synthesized publish-time DATE with namePrefixSource: "import-time"
    legacy_meeting = {
        "version": 1,
        "revision": 1,
        "id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
        "title": "Legacy unknown-start clip",
        "titleSource": "user",
        "notes": [],
        "speakers": [
            {"id": "spk-1", "name": "Sam", "provenance": "user", "colourIndex": 0},
        ],
        "transcript": {
            "status": "done",
            "reference": {
                "kind": "embedded",
                "file": "legacy-clip.opus",
                "transcriptId": "main",
            },
            "model": "whistle",
        },
        "etaMinutes": None,
        "error": None,
        "quoteOverride": None,
        "received": {
            "originalName": "legacy-clip.opus",
            "originalFolderName": "Downloads",
            "sha256": "1" * 64,
            "importedAtMs": 1795508100000,
        },
        "namePrefix": "2026-11-24--09.15-",
        "namePrefixSource": "import-time",
    }
    legacy_words = {
        "format": "cassini.words.v1",
        "language": "en",
        "items": [
            {"speaker": "spk-1", "startMs": 500, "endMs": 3000, "text": "Imported without a recording date."},
        ],
    }
    legacy_manifest_speakers = [{"id": "spk-1", "label": "Sam"}]
    # Pre-D112 writers (under superseded S-12 §11 step-3a #8) could write publish time into DATE.
    legacy_opus = build_opus(
        meeting_json=legacy_meeting,
        words=legacy_words,
        manifest_speakers=legacy_manifest_speakers,
        duration_ms=8000,
        created_at_utc="2026-11-24T08:15:00Z",
        date_tag="2026-11-24T09:15:00+01:00",
        recorded_at_local=None,
        manifest_started_at=None,
        automatic_title="legacy-clip",
    )
    legacy_expected = {
        "vector": "legacy-import-time-date",
        "decision": "D112 (L-87, supersedes S-12 §11 step-3a #8)",
        "rebuiltMeetingJson": legacy_meeting,
        "legacyDateTagPresent": "2026-11-24T09:15:00+01:00",
        "manifestCreatedAtUtc": "2026-11-24T08:15:00Z",
        "namePrefixSource": "import-time",
        "legacySynthesizedDateExcluded": True,
        "resolvedStartMs": None,
        "inDateUnknownGroup": True,
    }

    def dump_json(obj: dict) -> bytes:
        return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    return {
        "background-voice.meeting.json": dump_json(bg_meeting),
        "background-voice.opus": bg_opus,
        "background-voice.expected.json": dump_json(bg_expected),
        "user-started-at.meeting.json": dump_json(date_meeting),
        "user-started-at.opus": date_opus,
        "user-started-at.expected.json": dump_json(date_expected),
        "legacy-import-time-date.meeting.json": dump_json(legacy_meeting),
        "legacy-import-time-date.opus": legacy_opus,
        "legacy-import-time-date.expected.json": dump_json(legacy_expected),
    }


def main() -> None:
    artifacts = build_all()
    sha_manifest: dict[str, dict] = {}
    for name, data in sorted(artifacts.items()):
        (ROOT / name).write_bytes(data)
        sha_manifest[name] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "source": "generated by build.py",
        }
    (ROOT / "SHA256.json").write_text(
        json.dumps(sha_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Built {len(artifacts)} round-trip vector files and SHA256.json")


if __name__ == "__main__":
    main()
