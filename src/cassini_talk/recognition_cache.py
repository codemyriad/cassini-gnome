# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1120: a content-addressed cache of raw recogniser output per segment PCM.

`model.transcribe(samples)` is a pure function of the verified model and the
exact float32 little-endian samples, so an entry is keyed on those alone:
`<sha256 of the samples>:<sample count>` in a file whose header names the
model identity. An entry holds the recogniser's own segment-relative words
(seconds from the segment's first sample), text and language, before any
D55 placement, G11 clamping, D63 presence filtering or R71 fallback; those
run after Stop on one codepath whether or not a segment came from here.

Pure Python: the GTK-side queue merges and writes the file, the recognition
child reads it. Any other format (including v1) reads as empty (D51).
"""

import hashlib
import json
import math
import os
import re
from pathlib import Path

FILE = ".recognition-cache.json"
FORMAT = "cassini.recognition-cache.v2"
MAX_BYTES = 16 * 1024 * 1024
MAX_SEGMENTS = 100000
MAX_WORDS = 10000
MAX_TEXT = 1024 * 1024
RATE = 16000
# A recogniser may place its last word a little past the PCM it was given;
# the placement clamps that. Anything further is not a timestamp it produced.
SLACK_SECONDS = 30
_KEY = re.compile(r"\A[0-9a-f]{64}:[1-9][0-9]{0,11}\Z")


def segment_key(pcm_sha256, sample_count):
    return f"{pcm_sha256}:{sample_count}"


def samples_key(samples):
    """The key of float32 little-endian samples (a NumPy array or raw bytes)."""
    raw = (
        samples if isinstance(samples, bytes | bytearray | memoryview) else samples.astype("<f4", copy=False).tobytes()
    )
    if len(raw) % 4:
        raise ValueError("float32 samples expected")
    return segment_key(hashlib.sha256(raw).hexdigest(), len(raw) // 4)


def whistle_identity(bundle):
    """Whistle's identity is its verified bundle pin set (weights, library, wrapper)."""
    try:
        pins = (Path(bundle) / "pins.json").read_bytes()
    except OSError:
        return None
    return "whistle@" + hashlib.sha256(pins).hexdigest()


def parakeet_identity(model_tag, runtime_pins):
    """Parakeet's identity is its checkpoint plus the pinned Sherpa/ONNX Runtime binaries.

    A runtime upgrade (decoding, timestamps, INT8 kernels) can change the output
    of the same checkpoint, so the hash of `sherpa-pins.json` is part of it.
    """
    try:
        pins = Path(runtime_pins).read_bytes()
    except OSError:
        return None
    return f"{model_tag}+sherpa@{hashlib.sha256(pins).hexdigest()}"


def is_valid_model_identity(model_identity):
    if not isinstance(model_identity, str) or not model_identity:
        return False
    if any(ch in model_identity for ch in ("\n", "\r", "\0")):
        return False
    try:
        return len(model_identity.encode("utf-8")) <= 256
    except UnicodeError:
        return False


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_entry(key, entry):
    if not isinstance(key, str) or not _KEY.match(key) or not isinstance(entry, dict):
        return False
    if set(entry) != {"text", "language", "words"}:
        return False
    text, language, words = entry["text"], entry["language"], entry["words"]
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return False
    if not isinstance(language, str) or not 0 < len(language) <= 32:
        return False
    if not isinstance(words, list) or len(words) > MAX_WORDS:
        return False
    limit = int(key.rsplit(":", 1)[1]) / RATE + SLACK_SECONDS
    return all(
        isinstance(word, dict)
        and set(word) == {"start", "end", "text"}
        and _number(word["start"])
        and _number(word["end"])
        and 0 <= word["start"] <= word["end"] <= limit
        and isinstance(word["text"], str)
        and len(word["text"]) <= MAX_TEXT
        for word in words
    )


def entry(result):
    """The cacheable form of one `model.transcribe` result."""
    return dict(
        text=result["text"],
        language=result.get("language") or "und",
        words=[
            dict(start=float(word["start"]), end=float(word["end"]), text=word.get("word", word.get("text", "")))
            for word in result.get("words", [])
        ],
    )


def _reject_constant(name):
    raise ValueError("non-finite JSON number: " + name)


def parse(raw, model):
    """The valid segments of a cache file for exactly this model, else {}."""
    if not isinstance(model, str) or not model or len(raw) > MAX_BYTES:
        return {}
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    if not isinstance(value, dict) or value.get("format") != FORMAT or value.get("model") != model:
        return {}
    segments = value.get("segments")
    if not isinstance(segments, dict) or len(segments) > MAX_SEGMENTS:
        return {}
    return {key: item for key, item in segments.items() if valid_entry(key, item)}


def read(path, model):
    """Read `path` without following links; unreadable or foreign reads as {}."""
    from .library_files import regular

    if not isinstance(path, str | os.PathLike) or not str(path) or not isinstance(model, str) or not model:
        return {}
    target = Path(path)
    try:
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        with regular(descriptor, target.name) as (stream, _info):
            raw = stream.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        return {}
    finally:
        os.close(descriptor)
    return parse(raw, model)


def document(model, segments):
    return dict(format=FORMAT, model=model, segments=segments)
