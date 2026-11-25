#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Verify the D112 (L-86, L-87) meeting.json -> .opus -> meeting.json round-trip vectors."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build as rt_build  # noqa: E402

RFC3339_OFFSET_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:Z|\+(?:(?:0[0-9]|1[0-3]):[0-5][0-9]|14:00)|-(?:00:(?:0[1-9]|[1-5][0-9])|(?:0[1-9]|1[01]):[0-5][0-9]|12:00))$"
)
LOCAL_WALL_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$")


def read_opus_tags(path: Path) -> dict[str, str]:
    packets: list[bytes] = []
    partial = bytearray()
    with path.open("rb") as stream:
        while len(packets) < 2:
            header = stream.read(27)
            assert len(header) == 27 and header[:4] == b"OggS", path
            lacing = stream.read(header[26])
            payload = stream.read(sum(lacing))
            offset = 0
            for length in lacing:
                partial.extend(payload[offset : offset + length])
                offset += length
                if length < 255:
                    packets.append(bytes(partial))
                    partial.clear()
    pkt = packets[1]
    assert pkt[:8] == b"OpusTags", path
    vendor_len, = struct.unpack_from("<I", pkt, 8)
    offset = 12 + vendor_len
    count, = struct.unpack_from("<I", pkt, offset)
    offset += 4
    tags: dict[str, str] = {}
    for _ in range(count):
        size, = struct.unpack_from("<I", pkt, offset)
        offset += 4
        key, value = pkt[offset : offset + size].decode("utf-8").split("=", 1)
        offset += size
        assert key not in tags, f"duplicate tag {key}"
        tags[key] = value
    return tags


def decode_chunk_set(tags: dict[str, str], prefix: str) -> dict:
    count = int(tags[prefix + "CHUNK_COUNT"])
    encoded = "".join(tags[f"{prefix}{i:03d}"] for i in range(count))
    gz = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    assert len(gz) == int(tags[prefix + "GZIP_BYTES"])
    raw = gzip.decompress(gz)
    assert len(raw) == int(tags[prefix + "RAW_BYTES"])
    assert hashlib.sha256(raw).hexdigest() == tags[prefix + "SHA256"]
    return json.loads(raw.decode("utf-8"))


def rebuild_meeting_json(manifest: dict) -> dict:
    """Exact MAPPING.md rebuild rule: .opus manifest -> meeting.json."""
    x = manifest["x"]["codemyriad.io"]
    gc = manifest["x"].get("gocassini.com", {})
    rebuilt = {
        "id": x["sessionId"],
        "revision": x["revision"],
        "namePrefix": x["namePrefix"],
        "notes": gc.get("notes", []),
        **x["meeting"],
        **x.get("passthrough", {}),
    }
    if "namePrefixSource" in x:
        rebuilt["namePrefixSource"] = x["namePrefixSource"]
    return rebuilt


def verify_background_voice() -> None:
    meeting_in = json.loads((HERE / "background-voice.meeting.json").read_text(encoding="utf-8"))
    expected = json.loads((HERE / "background-voice.expected.json").read_text(encoding="utf-8"))
    tags = read_opus_tags(HERE / "background-voice.opus")
    manifest = decode_chunk_set(tags, "CASSINI_PAYLOAD_")
    words = decode_chunk_set(tags, "CASSINI_TX_MAIN_PAYLOAD_")

    # Round-trip meeting.json -> .opus -> meeting.json
    rebuilt = rebuild_meeting_json(manifest)
    assert rebuilt == meeting_in == expected["rebuiltMeetingJson"]

    # Schema constraints on background flag: const true when present, absent otherwise
    x_speakers = manifest["x"]["codemyriad.io"]["meeting"]["speakers"]
    for spk in x_speakers:
        if "background" in spk:
            assert spk["background"] is True, spk
    bg_ids = [s["id"] for s in x_speakers if s.get("background") is True]
    vis_ids = [s["id"] for s in x_speakers if not s.get("background")]
    assert bg_ids == expected["backgroundSpeakerIds"]
    assert vis_ids == expected["visibleSpeakerIds"]

    # Name is preserved on background speaker so toggling off restores it
    preserved = next(s for s in x_speakers if s["id"] == expected["preservedNameOnBackgroundSpeaker"]["id"])
    assert preserved["name"] == expected["preservedNameOnBackgroundSpeaker"]["name"]
    assert preserved["background"] is True

    # Manifest speakers[] keeps all original transcript speaker ids unchanged
    assert manifest["speakers"] == expected["manifestSpeakers"]
    assert tags["CASSINI_SPEAKER_COUNT"] == expected["speakerCountTag"]
    assert {item["speaker"] for item in words["items"]} == {s["id"] for s in manifest["speakers"]}

    # Row meta names leave out background speakers
    row_names = [s["name"] for s in x_speakers if not s.get("background") and s["name"]]
    assert row_names == expected["libraryRowNames"]

    # Folded consecutive background turns count distinct voices in that run
    bg_set = set(bg_ids)
    runs = []
    cur: list[str] = []
    for item in words["items"]:
        if item["speaker"] in bg_set:
            if item["speaker"] not in cur:
                cur.append(item["speaker"])
        elif cur:
            runs.append({"count": len(cur), "speakerIds": cur})
            cur = []
    if cur:
        runs.append({"count": len(cur), "speakerIds": cur})
    assert runs == expected["foldedRuns"]


def verify_user_started_at() -> None:
    meeting_in = json.loads((HERE / "user-started-at.meeting.json").read_text(encoding="utf-8"))
    expected = json.loads((HERE / "user-started-at.expected.json").read_text(encoding="utf-8"))
    tags = read_opus_tags(HERE / "user-started-at.opus")
    manifest = decode_chunk_set(tags, "CASSINI_PAYLOAD_")
    decode_chunk_set(tags, "CASSINI_TX_MAIN_PAYLOAD_")

    # Round-trip meeting.json -> .opus -> meeting.json
    rebuilt = rebuild_meeting_json(manifest)
    assert rebuilt == meeting_in == expected["rebuiltMeetingJson"]

    x = manifest["x"]["codemyriad.io"]
    x_meeting = x["meeting"]
    assert x_meeting["startedAtSource"] == "user"
    assert RFC3339_OFFSET_RE.fullmatch(x_meeting["startedAt"])
    assert not x_meeting["startedAt"].endswith("-00:00")
    assert "startedAt" not in x.get("passthrough", {})
    assert "startedAtSource" not in x.get("passthrough", {})

    # Projected DATE, manifest meeting.startedAt, and recordedAtLocal (no offset/zone/fraction)
    assert tags["DATE"] == expected["dateTag"] == x_meeting["startedAt"]
    assert manifest["meeting"]["startedAt"] == expected["manifestStartedAt"] == x_meeting["startedAt"]
    rec_local = manifest["meeting"]["recordedAtLocal"]
    assert LOCAL_WALL_RE.fullmatch(rec_local)
    assert rec_local == expected["manifestRecordedAtLocal"] == x_meeting["startedAt"][:19]

    # createdAtUtc and namePrefix/namePrefixSource are untouched (no rename)
    assert manifest["meeting"]["createdAtUtc"] == expected["manifestCreatedAtUtc"]
    assert x["namePrefix"] == expected["namePrefixUnchanged"]
    assert x["namePrefixSource"] == expected["namePrefixSourceUnchanged"]


def verify_legacy_import_time_date() -> None:
    meeting_in = json.loads((HERE / "legacy-import-time-date.meeting.json").read_text(encoding="utf-8"))
    expected = json.loads((HERE / "legacy-import-time-date.expected.json").read_text(encoding="utf-8"))
    tags = read_opus_tags(HERE / "legacy-import-time-date.opus")
    manifest = decode_chunk_set(tags, "CASSINI_PAYLOAD_")
    decode_chunk_set(tags, "CASSINI_TX_MAIN_PAYLOAD_")

    rebuilt = rebuild_meeting_json(manifest)
    assert rebuilt == meeting_in == expected["rebuiltMeetingJson"]

    x = manifest["x"]["codemyriad.io"]
    x_meeting = x["meeting"]
    assert x["namePrefixSource"] == "import-time" == expected["namePrefixSource"]
    assert "startedAt" not in x_meeting and "startedAtSource" not in x_meeting
    assert tags["DATE"] == expected["legacyDateTagPresent"]
    assert manifest["meeting"]["createdAtUtc"] == expected["manifestCreatedAtUtc"]
    assert "startedAt" not in manifest["meeting"] and "recordedAtLocal" not in manifest["meeting"]
    assert expected["legacySynthesizedDateExcluded"] is True
    assert expected["resolvedStartMs"] is None
    assert expected["inDateUnknownGroup"] is True


def verify_schema_offset_pattern() -> None:
    schema_path = HERE.parents[2] / "contract" / "meeting.schema.json"
    if schema_path.is_file():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert schema["properties"]["startedAt"]["pattern"] == RFC3339_OFFSET_RE.pattern
    for bad in (
        "2026-11-23T14:36:00-00:00",
        "2026-11-23T14:36:00-12:01",
        "2026-11-23T14:36:00-13:00",
        "2026-11-23T14:36:00-14:59",
        "2026-11-23T14:36:00+14:01",
        "2026-11-23T14:36:00+14:59",
    ):
        assert not RFC3339_OFFSET_RE.fullmatch(bad), f"must reject {bad}"
    for good in (
        "2026-11-23T14:36:00Z",
        "2026-11-23T14:36:00+00:00",
        "2026-11-23T14:36:00-00:01",
        "2026-11-23T14:36:00-12:00",
        "2026-11-23T14:36:00+14:00",
    ):
        assert RFC3339_OFFSET_RE.fullmatch(good), f"must accept {good}"


def main() -> None:
    # Determinism: build.py must reproduce exact committed bytes
    rebuilt_files = rt_build.build_all()
    sha_manifest = json.loads((HERE / "SHA256.json").read_text(encoding="utf-8"))
    assert set(rebuilt_files) == set(sha_manifest)
    for name, expected_bytes in sorted(rebuilt_files.items()):
        actual_bytes = (HERE / name).read_bytes()
        assert actual_bytes == expected_bytes, f"{name} differs from build.py output"
        assert len(actual_bytes) == sha_manifest[name]["bytes"]
        assert hashlib.sha256(actual_bytes).hexdigest() == sha_manifest[name]["sha256"]

    verify_schema_offset_pattern()
    verify_background_voice()
    verify_user_started_at()
    verify_legacy_import_time_date()
    print(
        "Verified 3 D112 round-trip vectors (background-voice, user-started-at, legacy-import-time-date), "
        "strict Q17 offset bounds, byte-identical builds and SHA256 pins"
    )


if __name__ == "__main__":
    main()
