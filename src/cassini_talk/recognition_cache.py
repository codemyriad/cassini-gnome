# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-stdlib TranscriptionMode seam and cassini.recognition-cache.v2 helpers (D109).

Kept free of NumPy, ONNX Runtime, and GStreamer so both the GTK parent process
and background speech workers share one exact cache key and validation schema.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from enum import StrEnum
from pathlib import Path

from .library_files import regular

RATE = 16000
RECOGNITION_CACHE_FILE = ".recognition-cache.json"
RECOGNITION_CACHE_FORMAT = "cassini.recognition-cache.v2"

MAX_CACHE_FILE_BYTES = 16 * 1024 * 1024
MAX_CACHE_SEGMENTS = 100_000
MAX_CACHED_WORDS = 10_000
MAX_MODEL_IDENTITY_BYTES = 256
MAX_LANGUAGE_BYTES = 32
MAX_SEGMENT_SAMPLES = RATE * 86_400
MAX_RELATIVE_WORD_MS = 86_400_000

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class TranscriptionMode(StrEnum):
    RECORD_ONLY = "record_only"
    AFTER_STOP = "after_stop"
    TRANSCRIBE_WHILE_RECORDING = "transcribe_while_recording"


_MODE_ALIASES: dict[str, TranscriptionMode] = {
    "recordOnly": TranscriptionMode.RECORD_ONLY,
    "record_only": TranscriptionMode.RECORD_ONLY,
    "RECORD_ONLY": TranscriptionMode.RECORD_ONLY,
    "afterStop": TranscriptionMode.AFTER_STOP,
    "after_stop": TranscriptionMode.AFTER_STOP,
    "AFTER_STOP": TranscriptionMode.AFTER_STOP,
    "transcribeWhileRecording": TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    "transcribe_while_recording": TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    "TRANSCRIBE_WHILE_RECORDING": TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
}


def normalize_transcription_mode(raw: object) -> TranscriptionMode:
    """Resolve persisted or caller-supplied mode values safely; default after_stop."""
    if isinstance(raw, TranscriptionMode):
        return raw
    if isinstance(raw, str):
        found = _MODE_ALIASES.get(raw)
        if found is not None:
            return found
    return TranscriptionMode.AFTER_STOP


def is_valid_model_identity(model_identity: object) -> bool:
    if not isinstance(model_identity, str) or not model_identity:
        return False
    if any(ch in model_identity for ch in ("\n", "\r", "\0")):
        return False
    try:
        return len(model_identity.encode("utf-8")) <= MAX_MODEL_IDENTITY_BYTES
    except UnicodeError:
        return False


def is_hex64(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64_RE.fullmatch(value))


def derive_segment_cache_key(model_identity: str, pcm_sha256: str, sample_count: int) -> str:
    """Derive the canonical cassini.recognition-cache.v2 64-hex key."""
    if not is_valid_model_identity(model_identity):
        raise ValueError("Invalid recognition cache model identity")
    if not is_hex64(pcm_sha256):
        raise ValueError("Invalid segment PCM SHA-256")
    if type(sample_count) is not int or not (1 <= sample_count <= MAX_SEGMENT_SAMPLES):
        raise ValueError("Invalid segment sample count")
    preimage = f"{RECOGNITION_CACHE_FORMAT}\n{model_identity}\n{pcm_sha256}\n{sample_count}"
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def normalize_language(language: object) -> str:
    if isinstance(language, str) and language:
        try:
            encoded = language.encode("utf-8")
        except UnicodeError:
            return "und"
        if 0 < len(encoded) <= MAX_LANGUAGE_BYTES:
            return language
    return "und"


def raw_seconds_to_relative_words(raw_words: object) -> list[dict] | None:
    """Convert raw recognizer seconds to validated integer relativeWords, or None if invalid."""
    if not isinstance(raw_words, list) or len(raw_words) > MAX_CACHED_WORDS:
        return None
    out: list[dict] = []
    for word in raw_words:
        if not isinstance(word, dict):
            return None
        start_s = word.get("start")
        end_s = word.get("end")
        word_text = word.get("word", word.get("text"))
        if (
            type(start_s) not in (int, float)
            or type(end_s) not in (int, float)
            or not math.isfinite(start_s)
            or not math.isfinite(end_s)
            or start_s < 0
            or end_s < start_s
            or end_s > 86400.0
            or not isinstance(word_text, str)
            or not word_text
        ):
            return None
        start_ms = round(start_s * 1000)
        end_ms = max(start_ms, round(end_s * 1000))
        if not (0 <= start_ms <= end_ms <= MAX_RELATIVE_WORD_MS):
            return None
        out.append(dict(startMs=start_ms, endMs=end_ms, text=word_text))
    return out


def valid_cached_entry(
    key: object,
    entry: object,
    *,
    model: str | None = None,
    pcm_sha256: str | None = None,
    sample_count: int | None = None,
) -> bool:
    """Validate one cassini.recognition-cache.v2 entry strictly per D109 §4.3."""
    if not is_hex64(key) or not isinstance(entry, dict):
        return False
    count = entry.get("sampleCount")
    text = entry.get("text")
    language = entry.get("language")
    words = entry.get("relativeWords")
    if (
        type(count) is not int
        or not (1 <= count <= MAX_SEGMENT_SAMPLES)
        or not isinstance(text, str)
        or not isinstance(language, str)
        or not language
        or not isinstance(words, list)
        or len(words) > MAX_CACHED_WORDS
    ):
        return False
    try:
        if len(language.encode("utf-8")) > MAX_LANGUAGE_BYTES:
            return False
    except UnicodeError:
        return False
    for word in words:
        if (
            not isinstance(word, dict)
            or type(word.get("startMs")) is not int
            or type(word.get("endMs")) is not int
            or not (0 <= word["startMs"] <= word["endMs"] <= MAX_RELATIVE_WORD_MS)
            or not isinstance(word.get("text"), str)
            or not word["text"]
        ):
            return False
    if sample_count is not None and count != sample_count:
        return False
    if model is not None and pcm_sha256 is not None and sample_count is not None:
        try:
            expected = derive_segment_cache_key(model, pcm_sha256, sample_count)
        except ValueError:
            return False
        if key != expected:
            return False
    return True


def cache_entry_payload(entry: dict) -> dict:
    return {
        "sampleCount": entry["sampleCount"],
        "text": entry["text"],
        "language": entry["language"],
        "relativeWords": [
            {"startMs": w["startMs"], "endMs": w["endMs"], "text": w["text"]} for w in entry["relativeWords"]
        ],
    }


def read_recognition_cache_file(path: str | Path, model: str) -> dict[str, dict]:
    """Read and validate a cassini.recognition-cache.v2 file safely (O_NOFOLLOW)."""
    if not isinstance(path, (str, Path)) or not str(path) or not is_valid_model_identity(model):
        return {}
    target = Path(path)
    try:
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        with regular(descriptor, target.name) as (stream, _info):
            raw = stream.read(MAX_CACHE_FILE_BYTES + 1)
    except (OSError, ValueError):
        return {}
    finally:
        os.close(descriptor)
    if len(raw) > MAX_CACHE_FILE_BYTES:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict) or value.get("format") != RECOGNITION_CACHE_FORMAT or value.get("model") != model:
        return {}
    segments = value.get("segments")
    if not isinstance(segments, dict) or len(segments) > MAX_CACHE_SEGMENTS:
        return {}
    return {k: cache_entry_payload(v) for k, v in segments.items() if valid_cached_entry(k, v)}


def whistle_model_identity(bundle: str | Path | None) -> str:
    """Return the verified Whistle weights identity (`whistle@<sha256>`) when present."""
    if bundle is None:
        return "whistle"
    manifest_path = Path(bundle) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return "whistle"
    if isinstance(manifest, dict):
        digest = manifest.get("whistle.cact")
        if is_hex64(digest):
            return f"whistle@{digest}"
    return "whistle"
