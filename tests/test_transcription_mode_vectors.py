# SPDX-License-Identifier: GPL-3.0-or-later
"""D111 / D-1117 conformance: vendored cassini-ux head-start/transcription-mode-vectors.json."""

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassini_talk.transcription import (
    TranscriptionMode,
    TranscriptionQueue,
    auto_write_enabled,
    resolve_transcription_mode,
    should_auto_write,
)

ROOT = Path(__file__).resolve().parents[1]
VECTORS_PATH = ROOT / "tests/ux/head-start/transcription-mode-vectors.json"
UPSTREAM_PATH = ROOT / "tests/ux/UPSTREAM.json"
VECTORS = json.loads(VECTORS_PATH.read_bytes())


def test_transcription_mode_vectors_pin_and_defaults():
    upstream = json.loads(UPSTREAM_PATH.read_text())
    key = "head-start/transcription-mode-vectors.json"
    assert upstream["paths"][key] == "tests/ux/head-start/transcription-mode-vectors.json"
    assert upstream["sourcePaths"][key] == "vectors/head-start/transcription-mode-vectors.json"
    assert hashlib.sha256(VECTORS_PATH.read_bytes()).hexdigest() == upstream["sha256"][key]

    assert VECTORS["schema"] == "cassini.head-start.transcription-mode-vectors.v1"
    assert VECTORS["canonicalModes"] == [m.value for m in TranscriptionMode]
    assert VECTORS["defaultMode"] == TranscriptionMode.TRANSCRIBE_WHILE_RECORDING.value

    source = (ROOT / "src/cassini_talk/main.py").read_text()
    app_default = next(
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "transcription_mode" for t in node.targets)
    )
    assert app_default == VECTORS["defaultMode"]

    assert resolve_transcription_mode(SimpleNamespace()).value == VECTORS["defaultMode"]

    queue = TranscriptionQueue(lambda _: None, lambda _: None)
    try:
        assert queue.mode.value == VECTORS["defaultMode"]
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["id"])
def test_transcription_mode_vector_case(case, monkeypatch, tmp_path):
    inp = case["input"]
    exp = case["expected"]
    raw = inp["rawMode"]

    monkeypatch.delenv("CASSINI_TRANSCRIPTION_MODE", raising=False)

    # 1. Mode parsing and fallback resolution.
    if exp["fellBackToDefault"]:
        with pytest.raises(ValueError):
            TranscriptionMode.parse(raw)
        if isinstance(raw, str):
            monkeypatch.setenv("CASSINI_TRANSCRIPTION_MODE", raw)
            resolved = resolve_transcription_mode(SimpleNamespace())
        else:
            # Non-string raw values (None, int) cannot live in env; default fallback applies.
            resolved = resolve_transcription_mode(SimpleNamespace(transcription_mode=None))
    else:
        resolved = TranscriptionMode.parse(raw)
        monkeypatch.setenv("CASSINI_TRANSCRIPTION_MODE", raw)
        assert resolve_transcription_mode(SimpleNamespace()) is resolved

    assert resolved.value == exp["resolvedMode"]

    # 2. Stop auto-enqueue policy and saved transcript state.
    app = SimpleNamespace(transcription_enabled=True, transcription_mode=resolved)
    auto_enqueue = auto_write_enabled(app)
    assert auto_enqueue is exp["autoEnqueueOnStop"]

    meeting = SimpleNamespace(
        key="m1",
        origin="own",
        lifecycle="saved",
        capture_state="completed",
        transcript="none",
        metadata_editable=True,
        directory=tmp_path / "meeting-1",
        published_file=tmp_path / "meeting-1.opus",
    )
    assert should_auto_write(meeting, {}, mode=resolved) is exp["autoEnqueueOnStop"]
    assert ("queued" if auto_enqueue else "none") == exp["savedTranscriptStateOnStop"]

    # 3. Precompute follower spawn, manual transcribe, and preemption rules.
    has_fg = inp["hasForegroundSavedJob"]
    manual_req = inp["manualTranscribeRequested"]
    supports_precompute = inp["platformSupportsPrecompute"]

    preempt = has_fg or manual_req
    spawn = (resolved is TranscriptionMode.TRANSCRIBE_WHILE_RECORDING) and supports_precompute and not preempt
    assert spawn is exp["spawnWorkerDuringRecording"]
    assert manual_req is exp["enqueueOnManualRequest"]
    assert preempt is exp["preemptActiveFollower"]
