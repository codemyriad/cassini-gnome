#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic offline speech worker child for D-1121 subprocess integration tests.

Exercises the exact production background_worker_priority(), StdinLineReader,
run_follow_loop(), process_tracks() (including SegmentationPrefix reuse and
cassini.recognition-cache.v2), and Whistle/Parakeet warm worker handoff protocol
without requiring external downloaded model weights.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cassini_talk.parakeet_model import MODEL_TAG as PARAKEET_MODEL_TAG  # noqa: E402
from cassini_talk.session_follower import StdinLineReader, run_follow_loop  # noqa: E402
from cassini_talk.speech_pipeline import process_tracks  # noqa: E402
from cassini_talk.speech_threads import background_worker_priority  # noqa: E402
from cassini_talk.whistle_worker import emit  # noqa: E402


class DeterministicDetector:
    """Fast stateful VAD with exact snapshot/restore semantics (same interface as SpeechDetector)."""

    frame_size = 512
    input_stage = "test-energy-vad-v1"

    def __init__(self, *_args):
        self.runtime = dict(inputStage=self.input_stage, version="test", providers=["CPU"], intraThreads=1, interThreads=1)
        self.prob_calls = 0
        self.reset()

    def reset(self):
        self.hpf = (0.0, 0.0, 0.0, 0.0)
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def snapshot(self):
        return (self.hpf, self.state.copy(), self.context.copy())

    def restore(self, snap):
        hpf, state, context = snap
        self.hpf = tuple(hpf)
        self.state = state.copy()
        self.context = context.copy()

    def probability(self, frame):
        self.prob_calls += 1
        # Include simple deterministic state evolution so restoring state matters.
        rms = float(np.sqrt(np.mean(np.asarray(frame, dtype=np.float64) ** 2)))
        prev = self.hpf[0]
        blended = 0.85 * rms + 0.15 * prev
        self.hpf = (blended, prev, 0.0, 0.0)
        self.state[0, 0, 0] = np.float32(blended)
        self.context[0, :] = np.asarray(frame[-64:], dtype=np.float32)
        return 0.9 if blended >= 0.02 else 0.05


class DeterministicRecognizer:
    """Pure function of (engine, samples_f32_le) matching Whistle/Parakeet contract."""

    initial_realtime_factor = 0.05

    def __init__(self, engine: str):
        self.engine = engine
        self.calls = 0

    def transcribe(self, samples, language=None, word_timestamps=True):
        assert language is None and word_timestamps is True
        self.calls += 1
        arr = np.asarray(samples, dtype="<f4")
        digest = hashlib.sha256(arr.tobytes()).hexdigest()[:8]
        dur = max(0.05, len(arr) / 16000.0)
        w1_end = round(min(dur * 0.45, dur), 3)
        w2_start = round(min(dur * 0.5, dur), 3)
        w2_end = round(min(dur * 0.92, dur), 3)
        t1 = f"{self.engine}_{digest}"
        t2 = f"n{len(arr)}"
        return dict(
            text=f"{t1} {t2}",
            language="en",
            words=[
                dict(word=t1, start=0.02, end=max(0.02, w1_end)),
                dict(word=t2, start=w2_start, end=max(w2_start, w2_end)),
            ],
        )


def _emit_fake_diarization(tracks, options, emit_fn):
    """Emit deterministic valid speaker-turns groups when diarization is requested."""
    if not options:
        return None
    from cassini_talk.speech_pipeline import audio_chunks

    group_ids = []
    for track in tracks:
        if track.get("silent") is True:
            continue
        gid = track["speaker"]
        if gid in group_ids:
            continue
        group_ids.append(gid)
        dur_ms = max(1000, int((track.get("frames") or 16000) * 1000 // max(1, int(track.get("sampleRate") or 16000))))
        digest = hashlib.sha256()
        for block in audio_chunks(track):
            digest.update(np.asarray(block, dtype="<f4").tobytes())
        input_sha = digest.hexdigest()
        turns = [dict(startMs=0, endMs=dur_ms, speaker=0)]
        emit_fn(
            "diarized",
            groupId=gid,
            first=True,
            final=True,
            reused=False,
            durationMs=dur_ms,
            inputSHA256=input_sha,
            turns=turns,
        )
    return dict(
        model="fake-nemotron@0",
        params=dict(threshold=0.5),
        groupIds=group_ids,
        realtimeFactor=0.01,
        diagnostics=[],
    )


def main():
    engine = sys.argv[1]
    bundle = Path(sys.argv[2])
    background_worker_priority()
    model_identity = PARAKEET_MODEL_TAG if engine == "parakeet" else "whistle@" + ("a" * 64)
    model = DeterministicRecognizer(engine)
    detector = DeterministicDetector()
    prefix_store = {}
    reader = StdinLineReader(sys.stdin.fileno())
    emit("ready", pid=os.getpid(), engine=engine, modelIdentity=model_identity)
    try:
        while line := reader.readline():
            message = json.loads(line)
            msg_type = message.get("type")
            if msg_type == "quit":
                return 0
            if msg_type == "follow":
                action, detector = run_follow_loop(
                    model, bundle, message, emit, reader=reader, detector=detector, prefix_store=prefix_store
                )
                if action != "continue":
                    return 0
                continue
            if msg_type == "follow-stop":
                emit("follow-stopped", directory=message.get("directory"), segments=0, prefixes=len(prefix_store))
                continue
            if msg_type == "pipeline":
                tracks = message["tracks"]
                calls_before = model.calls
                vad_before = detector.prob_calls
                try:
                    summary = process_tracks(
                        model,
                        tracks,
                        bundle / "silero_vad.onnx",
                        emit,
                        extra_seconds=0,
                        recognition_cache=message.get("recognitionCache"),
                        detector=detector,
                        prefix_store=prefix_store,
                    )
                finally:
                    prefix_store.clear()
                diar_result = _emit_fake_diarization(tracks, message.get("diarization"), emit)
                if diar_result is not None:
                    summary["diarization"] = diar_result
                summary["workerPid"] = os.getpid()
                summary["postStopAsrCalls"] = model.calls - calls_before
                summary["postStopVadFrames"] = detector.prob_calls - vad_before
                emit("complete", **summary)
                if engine == "parakeet":
                    return 0
                continue
            raise ValueError(f"Unsupported fake worker message: {msg_type}")
        return 0
    finally:
        reader.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        emit("error", message=str(error))
        sys.exit(1)
