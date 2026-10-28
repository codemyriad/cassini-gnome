#!/usr/bin/env python3
"""Read a Cassini portable meeting with no external tools at all.

Walks the Ogg pages and the OpusTags comment packet itself: standard library,
nothing else. Everything it needs lives in the first two packets, so it reads
the front of the file and grows the read until the comment header is complete.
The same code reads a meeting over an HTTP Range request without downloading
the audio: a short read raises Truncated, which is the signal to ask for more.

It is a metadata-profile reader. It verifies every payload digest and never
the audio, so the best state it can report is `unverified`.

    python3 cassini-read-pure.py meeting.opus

SPDX-License-Identifier: CC0-1.0
"""
import base64, binascii, hashlib, json, struct, sys, zlib

FIRST_READ = 1 << 20        # enough for most files; doubled until OpusTags ends
INFLATE_CEILING = 64 << 20  # no chunk set legitimately inflates past this
KNOWN_MAJOR = "org.cassini.portable-meeting/1"


class Truncated(ValueError):
    """The bytes end before OpusTags does: read more and try again."""


class Invalid(ValueError):
    """Cassini metadata is present and cannot be trusted as a whole."""


def load_bearing(name):
    """The tags a repeat makes the file invalid for; the rest are copies."""
    return name == "CASSINI_FORMAT" or name.startswith(("CASSINI_PAYLOAD_", "CASSINI_TX_"))


def opus_tags(data):
    """Ogg bytes -> (vendor, {FIELD: value}, bytes_consumed). Names upper-cased.

    Reassemble PACKETS, not pages. A page holds at most 255 x 255 bytes and a
    comment header carrying a transcript is routinely bigger, so OpusTags spans
    several pages. A lacing value below 255 ends a packet.
    """
    packets, partial, pos = [], b"", 0
    while len(packets) < 2:
        if pos + 27 > len(data):
            raise Truncated("truncated before the comment header ended")
        if data[pos:pos + 4] != b"OggS":
            raise ValueError(f"not an Ogg stream at byte {pos}")
        nsegs = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nsegs]
        cur = pos + 27 + nsegs
        pos = cur + sum(lacing)
        if pos > len(data):
            raise Truncated("truncated before the comment header ended")
        for n in lacing:
            partial += data[cur:cur + n]
            cur += n
            if n < 255:
                packets.append(partial)
                partial = b""
    if packets[0][:8] != b"OpusHead" or packets[1][:8] != b"OpusTags":
        raise ValueError("not an Ogg Opus stream")

    pkt = packets[1]
    vendor_len, = struct.unpack_from("<I", pkt, 8)
    off = 12 + vendor_len
    vendor = pkt[12:off].decode("utf-8", "replace")
    count, = struct.unpack_from("<I", pkt, off)
    off += 4
    tags = {}
    for _ in range(count):
        size, = struct.unpack_from("<I", pkt, off)
        off += 4
        name, _, value = pkt[off:off + size].decode("utf-8").partition("=")
        off += size
        name = name.upper()
        if name in tags:
            if load_bearing(name):
                raise Invalid(f"{name} appears twice; a Cassini file never repeats it")
            continue                        # a repeated copy: keep the first
        tags[name] = value
    return vendor, tags, pos


def chunks(t, prefix, count, sha256=None, raw_bytes=None):
    """PREFIX_000..N-1 -> base64url -> gzip -> UTF-8 JSON, inflate bounded."""
    missing = [i for i in range(count) if f"{prefix}{i:03d}" not in t]
    if missing:
        raise ValueError(f"{prefix}*: missing chunk {prefix}{missing[0]:03d}")
    b64 = "".join(t[f"{prefix}{i:03d}"] for i in range(count)).rstrip("=")
    limit = min(raw_bytes if raw_bytes is not None else INFLATE_CEILING, INFLATE_CEILING)
    try:
        gz = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = inflater.decompress(gz, limit + 1)
    except (binascii.Error, zlib.error) as exc:
        raise ValueError(f"{prefix}*: payload will not decode: {exc}") from None
    if len(body) > limit:
        raise ValueError(f"{prefix}*: inflates past the declared {limit} bytes")
    if not inflater.eof:
        raise ValueError(f"{prefix}*: gzip stream is truncated")
    got = hashlib.sha256(body).hexdigest()
    if sha256 and got != sha256.lower():
        raise ValueError(f"{prefix}*: sha256 {got} != declared {sha256}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{prefix}*: payload is not JSON: {exc}") from None


def read(data):
    """Ogg bytes -> dict with state, manifest, words, transcript notes, bytes_used."""
    try:
        _vendor, t, used = opus_tags(data)
    except Invalid as exc:
        return {"state": "invalid-cassini-metadata", "why": str(exc), "used": len(data)}
    fmt = (t.get("CASSINI_FORMAT") or "").strip()
    if not fmt:
        return {"state": "plain-audio", "used": used}
    if fmt.lower() != KNOWN_MAJOR:
        return {"state": "unknown-cassini-format", "format": fmt, "used": used}
    try:
        manifest = chunks(t, "CASSINI_PAYLOAD_", int(t["CASSINI_PAYLOAD_CHUNK_COUNT"]),
                          t.get("CASSINI_PAYLOAD_SHA256"),
                          int(t["CASSINI_PAYLOAD_RAW_BYTES"]))
        if manifest.get("kind") != "cassini-portable-meeting" or manifest.get("version") != 1:
            raise ValueError("manifest kind or version is not the one CASSINI_FORMAT names")
    except (KeyError, ValueError) as exc:
        return {"state": "invalid-cassini-metadata", "why": str(exc), "used": used}

    # The words slot: first entry flagged default, else the first entry.
    # CASSINI_TRANSCRIPT_DEFAULT is a copy and does not decide.
    entries = manifest.get("transcripts") or []
    want = next((e["id"] for e in entries if e.get("default")), None) \
        or (entries[0]["id"] if entries else None)
    words, notes = None, []
    tagged = (t.get("CASSINI_TRANSCRIPT_DEFAULT") or "").strip()
    if tagged and want and tagged != want:
        notes.append(f"CASSINI_TRANSCRIPT_DEFAULT says {tagged!r}; the manifest resolves {want!r}")
    for e in entries:
        ref = e["payloadRef"]
        try:
            body = chunks(t, ref["prefix"], ref["chunkCount"], ref["sha256"], ref.get("rawBytes"))
            notes.append(f"{e['id']}: available, {len(body['items'])} items")
            if e["id"] == want:
                words = body["items"]
        except (KeyError, ValueError, TypeError) as exc:
            notes.append(f"{e['id']}: unavailable, {exc}")     # this body, not the file
    return {"state": "unverified", "manifest": manifest, "words": words,
            "default": want, "notes": notes, "used": used}


def read_file(path):
    """Grow the prefix read until OpusTags is complete."""
    with open(path, "rb") as fh:
        total = fh.seek(0, 2)
        size = FIRST_READ
        while True:
            fh.seek(0)
            head = fh.read(size)
            try:
                return read(head), total
            except Truncated:
                if size >= total:
                    raise
                size *= 2


if __name__ == "__main__":
    try:
        r, total = read_file(sys.argv[1])
    except ValueError as exc:
        sys.exit(f"{sys.argv[1]}: {exc}")
    print(f"state: {r['state']}" + (f" ({r['why']})" if "why" in r else "")
          + (f" ({r['format']})" if "format" in r else ""))
    if r["state"] in ("plain-audio", "unknown-cassini-format", "invalid-cassini-metadata"):
        sys.exit(0)
    m = r["manifest"]
    print(f'{m["meeting"]["title"]} - {m["meeting"]["durationMs"] / 1000:.0f} s, '
          f'default transcript {r["default"]!r}')
    for note in r["notes"]:
        print(f"  {note}")
    print(f"all of it came from the first {r['used']} of {total} bytes "
          f"({100 * r['used'] / total:.2f}%); the audio was not checked")
    for w in (r["words"] or [])[:6]:
        print(f'{w["startMs"]:>8} ms  {w["speaker"]:<10} {w["text"]}')
