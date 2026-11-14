# SPDX-License-Identifier: GPL-3.0-or-later
"""W0.7 / L-25 shared audio import types (vendored contract/import-types.json).

GTK-free. Only entries with ``status == "decided"`` are offered. Picker filters
use ``mimeTypes`` only (GtkFileFilter matches MIME subclasses, so the broad
``compatibilityMimeTypes`` such as ``audio/ogg`` would also offer Ogg video
and FLAC-in-Ogg). Ambiguous ``.ogg`` inputs and inputs arriving under a
compatibility MIME type must pass ``contentValidation`` before import; entries
with a fixed native container (``.flac``, D102) must pass ``headerValidation``.
"""

import json
from functools import cache
from pathlib import Path, PurePath

CONTRACT = Path(__file__).with_name("import-types.json")
SCHEMA = "cassini-ux/import-types@1"
# Copied byte-for-byte; every other decided type is decoded into Ogg Opus.
OPUS_ID = "opus-ogg"

# Ogg page header (RFC 3533): capture pattern, version, header type, granule,
# serial, sequence, CRC, segment count, then the lacing table.
OGG_HEADER_BYTES = 27
OGG_BOS_FLAG = 0x02


@cache
def contract():
    payload = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("Unexpected import-types schema")
    return payload


def decided():
    return tuple(entry for entry in contract()["types"] if entry["status"] == "decided")


def _unique(values):
    return tuple(dict.fromkeys(values))


def picker_mime_types():
    """Safe MIME filters for subclass-matching pickers: decided ``mimeTypes`` only."""
    return _unique(mime for entry in decided() for mime in entry["mimeTypes"])


def extensions():
    return _unique(ext for entry in decided() for ext in entry["extensions"])


def labels():
    return tuple(entry["label"] for entry in decided())


def _validations():
    return tuple(entry["contentValidation"] for entry in decided() if "contentValidation" in entry)


def rejected_extensions():
    return _unique(ext for rule in _validations() for ext in rule["rejectedExtensions"])


def ambiguous_extensions():
    return _unique(ext for rule in _validations() for ext in rule["ambiguousExtensions"])


def entry_for(name):
    """The decided entry that owns ``name``'s extension, or None."""
    suffix = PurePath(str(name)).suffix.lower()
    if not suffix or suffix in rejected_extensions():
        return None
    return next((entry for entry in decided() if suffix in entry["extensions"]), None)


def needs_ogg_probe(name, mime=None):
    """True for ambiguous container extensions (.ogg) or compatibility-MIME inputs."""
    if PurePath(str(name)).suffix.lower() in ambiguous_extensions():
        return True
    if mime:
        mime = mime.split(";", 1)[0].strip().lower()
        return any(mime in entry["compatibilityMimeTypes"] for entry in decided())
    return False


def acceptable_name(name, mime=None):
    """Name/MIME check only (no content), as the contract's ``validate_import_candidate``.

    A MIME type must belong to the owning entry (``mimeTypes`` or
    ``compatibilityMimeTypes``) and not be one of its rejected types, so an
    Ogg-FLAC or broad Ogg MIME type on ``.flac``, or an Ogg-FLAC MIME type on
    ``.ogg``/``.opus``, is refused. Content probing is ``check_ogg_bytes`` /
    ``check_header_bytes``.
    """
    entry = entry_for(name)
    if entry is None:
        return False
    if mime:
        mime = mime.split(";", 1)[0].strip().lower()
        prefixes = _unique(prefix for rule in _validations() for prefix in rule["rejectedMimePrefixes"])
        if mime.startswith("video/") or any(mime.startswith(prefix) for prefix in prefixes):
            return False
        open_mimes = {m for e in contract()["types"] if e["status"] != "decided" for m in e["mimeTypes"]}
        if mime in open_mimes:
            return False
        if mime not in (*entry["mimeTypes"], *entry["compatibilityMimeTypes"]):
            return False
        if mime in entry.get("headerValidation", {}).get("rejectedMimeTypes", ()):
            return False
    return True


def header_validation(name):
    """The owning entry's ``headerValidation`` (native FLAC), or None."""
    entry = entry_for(name)
    return entry.get("headerValidation") if entry is not None else None


def check_header_bytes(data, rule):
    """Return None when ``data`` (the first ``minProbeBytes``) passes the native
    ``headerValidation`` rule (D102, RFC 9639), else a short reason code:
    ``ogg``, ``id3``, ``not-flac``, ``truncated``, ``not-streaminfo``,
    ``streaminfo-length`` or ``sample-rate``.
    """
    data = bytes(data)
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"ID3"):
        return "id3"
    if not data.startswith(bytes.fromhex(rule["magicHex"])):
        return "not-flac"
    if len(data) < rule["minProbeBytes"]:
        return "truncated"
    if data[4] & 0x7F != rule["firstMetadataBlockType"]:
        return "not-streaminfo"
    if int.from_bytes(data[5:8], "big") != rule["streamInfoLength"]:
        return "streaminfo-length"
    # STREAMINFO: min/max block size (2+2), min/max frame size (3+3), then a
    # 20-bit sample rate, i.e. bytes 18-20 of the stream.
    if (data[18] << 12) | (data[19] << 4) | (data[20] >> 4) == 0:
        return "sample-rate"
    return None


def check_header_file(stream, rule):
    """Probe an open binary stream from its start (it is rewound afterwards)."""
    position = stream.tell()
    try:
        stream.seek(0)
        return check_header_bytes(stream.read(rule["minProbeBytes"]), rule)
    finally:
        stream.seek(position)


def probe_bytes():
    return max((rule["minProbeBytes"] for rule in _validations()), default=1024)


def _ogg_page(data, offset):
    """Return (header_type, payload_start, next_page) or None if truncated/not OggS."""
    end = offset + OGG_HEADER_BYTES
    if len(data) < end or data[offset : offset + 4] != b"OggS" or data[offset + 4] != 0:
        return None
    segments = data[offset + 26]
    lacing = data[end : end + segments]
    if len(lacing) != segments:
        return None
    payload = end + segments
    return data[offset + 5], payload, payload + sum(lacing)


def check_ogg_bytes(data):
    """Return None when ``data`` (the first probe bytes) is an accepted Ogg Opus
    stream, else a short reason code: ``not-ogg``, ``rejected-stream``,
    ``not-opus`` or ``multiplexed``.

    Page 0 must be a BOS page whose payload starts with an accepted magic
    (OpusHead) and Page 1 a non-BOS page starting with OpusTags. A second BOS
    page (Theora/Skeleton/... multiplexed beside Opus) is rejected.
    """
    data = bytes(data)
    for rule in _validations():
        if data[:4] != bytes.fromhex(rule["containerMagicHex"]):
            return "not-ogg"
        first = _ogg_page(data, 0)
        if first is None or not first[0] & OGG_BOS_FLAG:
            return "not-ogg"
        head = data[first[1] : first[2]]
        if any(head.startswith(bytes.fromhex(magic)) for magic in rule["rejectedBosMagicHex"]):
            return "rejected-stream"
        if not any(head.startswith(bytes.fromhex(magic)) for magic in rule["acceptedBosMagicHex"]):
            return "not-opus"
        second = _ogg_page(data, first[2])
        if second is None:
            return "not-opus"
        if second[0] & OGG_BOS_FLAG:
            return "multiplexed"
        if not data[second[1] :].startswith(bytes.fromhex(rule["secondPageMagicHex"])):
            return "not-opus"
        return None
    return "not-ogg"


def check_ogg_file(stream):
    """Probe an open binary stream from its start (it is rewound afterwards)."""
    position = stream.tell()
    try:
        stream.seek(0)
        return check_ogg_bytes(stream.read(probe_bytes()))
    finally:
        stream.seek(position)


def is_plain_audio(name):
    """Decided non-Opus types that must be decoded into an Opus portable meeting."""
    entry = entry_for(name)
    return entry is not None and entry["id"] != OPUS_ID
