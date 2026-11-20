#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic offline speech worker child for D-1121 / D-1125 subprocess integration tests.

Exercises the exact production background_worker_priority(), StdinLineReader,
run_follow_loop() (including diarization pre-staging and WarmDiarizer),
process_tracks() (including SegmentationPrefix reuse and cassini.recognition-cache.v2),
worker_diarization.complete() (including StagedDiarization tail append and G12 SHA-256),
and Whistle/Parakeet warm worker handoff protocol without requiring external model weights.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cassini_talk import scratch_budget  # noqa: E402
from cassini_talk.diarization import normalize_turns  # noqa: E402
from cassini_talk.diarization_audio import close_diarization_store, discard_staged_for_track  # noqa: E402
from cassini_talk.session_follower import StdinLineReader, run_follow_loop  # noqa: E402
from cassini_talk.speech_pipeline import process_tracks  # noqa: E402
from cassini_talk.speech_threads import background_worker_priority  # noqa: E402
from cassini_talk.whistle_worker import emit  # noqa: E402
from cassini_talk.worker_diarization import WarmDiarizer, complete, estimate  # noqa: E402


def _configure_fake_statvfs() -> None:
    raw_free = os.environ.get("CASSINI_FAKE_FREE_BYTES")
    raw_total = os.environ.get("CASSINI_FAKE_TOTAL_BYTES")
    free = int(raw_free) if raw_free is not None else (1 << 50)
    total = int(raw_total) if raw_total is not None else (1 << 51)
    usage = SimpleNamespace(f_bavail=free, f_frsize=1, f_blocks=total)
    scratch_budget.volume_usage = lambda _path: usage


class DeterministicDetector:
    """Fast stateful VAD with exact snapshot/restore semantics (same interface as SpeechDetector)."""

    frame_size = 512
    input_stage = "test-energy-vad-v1"

    def __init__(self, *_args):
        self.runtime = dict(
            inputStage=self.input_stage, version="test", providers=["CPU"], intraThreads=1, interThreads=1
        )
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


class DeterministicNemotron:
    """Pure function of whole-group 16 kHz <f4 PCM separating <495 Hz (spk 0) vs >=495 Hz (spk 1)."""

    constructs = 0
    infers = 0
    closes = 0

    def __init__(self, model_path, runtime_path):
        if os.environ.get("CASSINI_FAKE_NEMOTRON_FAIL_INIT") == "1":
            raise RuntimeError("Simulated Nemotron init failure")
        type(self).constructs += 1
        self.model_path = str(model_path)
        self.runtime_path = str(runtime_path)
        self.open = True

    def infer(self, samples, notify=lambda *_: None):
        if not self.open or samples.ndim != 1 or samples.dtype.str != "<f4":
            raise ValueError("Expected open model and 1-D <f4 samples")
        type(self).infers += 1
        notify(1, 1)
        step = 1600  # 100 ms at 16 kHz
        raw = []
        for start in range(0, len(samples) - step + 1, step):
            window = np.asarray(samples[start : start + step], dtype=np.float64)
            rms = float(np.sqrt(np.mean(window * window)))
            if rms < 0.03:
                continue
            # Estimate dominant frequency via zero crossings (2 crossings per cycle).
            signs = window >= 0.0
            crossings = int(np.count_nonzero(signs[1:] != signs[:-1]))
            est_hz = crossings * (16000.0 / (2.0 * step))
            speaker = 0 if est_hz < 495.0 else 1
            sec_start, sec_end = start / 16000.0, (start + step) / 16000.0
            if raw and raw[-1]["speaker"] == speaker and abs(raw[-1]["end"] - sec_start) < 1e-6:
                raw[-1]["end"] = sec_end
            else:
                raw.append(dict(start=sec_start, end=sec_end, speaker=speaker))
        duration_ms = len(samples) * 1000 // 16000
        turns = normalize_turns(raw, duration_ms)
        return dict(
            turns=turns,
            speakerCount=len({t["speaker"] for t in turns}),
            inferenceSeconds=0.005,
        )

    def close(self):
        if self.open:
            self.open = False
            type(self).closes += 1


def _with_default_scratch(options: dict | None, bundle: Path) -> dict | None:
    if not isinstance(options, dict):
        return None
    if options.get("scratch"):
        return options
    return dict(options, scratch=str(bundle / "scratch"))


def main():
    from cassini_talk.parakeet_model import MODEL_ID as PARAKEET_MODEL_ID
    from cassini_talk.transcription import recognition_cache_model

    _configure_fake_statvfs()
    sys.modules["cassini_talk.nemotron"] = types.SimpleNamespace(Nemotron=DeterministicNemotron)

    engine = sys.argv[1]
    bundle = Path(sys.argv[2])
    background_worker_priority()
    model_identity = recognition_cache_model(PARAKEET_MODEL_ID if engine == "parakeet" else "whistle", bundle)
    model = DeterministicRecognizer(engine)
    detector = DeterministicDetector()
    prefix_store = {}
    diarization_store = {}
    warm_diarizer = WarmDiarizer()
    reader = StdinLineReader(sys.stdin.fileno())
    emit("ready", pid=os.getpid(), engine=engine, modelIdentity=model_identity)
    try:
        while line := reader.readline():
            message = json.loads(line)
            msg_type = message.get("type")
            if msg_type == "quit":
                return 0
            if msg_type == "follow":
                follow_msg = dict(message)
                if "diarization" in follow_msg:
                    follow_msg["diarization"] = _with_default_scratch(follow_msg["diarization"], bundle)
                action, detector = run_follow_loop(
                    model,
                    bundle,
                    follow_msg,
                    emit,
                    reader=reader,
                    detector=detector,
                    prefix_store=prefix_store,
                    diarization_store=diarization_store,
                    warm_diarizer=warm_diarizer,
                )
                if action != "continue":
                    return 0
                continue
            if msg_type == "follow-stop":
                if message.get("drain", True) is False:
                    prefix_store.clear()
                    close_diarization_store(diarization_store)
                    warm_diarizer.close()
                emit(
                    "follow-stopped",
                    directory=message.get("directory"),
                    segments=0,
                    prefixes=len(prefix_store),
                    diarizationStaged=len({id(v) for v in diarization_store.values()}),
                    diarizerWarmed=warm_diarizer.model is not None,
                )
                continue
            if msg_type == "pipeline":
                tracks = message["tracks"]
                options = _with_default_scratch(message.get("diarization"), bundle)
                calls_before = model.calls
                vad_before = detector.prob_calls
                constructs_before = DeterministicNemotron.constructs
                try:
                    try:
                        summary = process_tracks(
                            model,
                            tracks,
                            bundle / "silero_vad.onnx",
                            emit,
                            extra_seconds=estimate(tracks, options),
                            recognition_cache=message.get("recognitionCache"),
                            detector=detector,
                            prefix_store=prefix_store,
                            on_prefix_mismatch=lambda tr: discard_staged_for_track(diarization_store, tr),
                        )
                    finally:
                        prefix_store.clear()
                    summary = complete(
                        summary,
                        tracks,
                        options,
                        emit,
                        diarization_store=diarization_store,
                        warm_diarizer=warm_diarizer,
                    )
                finally:
                    close_diarization_store(diarization_store)
                    warm_diarizer.close()
                summary["workerPid"] = os.getpid()
                summary["postStopAsrCalls"] = model.calls - calls_before
                summary["postStopVadFrames"] = detector.prob_calls - vad_before
                summary["postStopNemotronConstructs"] = DeterministicNemotron.constructs - constructs_before
                summary["totalNemotronConstructs"] = DeterministicNemotron.constructs
                summary["totalNemotronCloses"] = DeterministicNemotron.closes
                emit("complete", **summary)
                if engine == "parakeet":
                    return 0
                continue
            raise ValueError(f"Unsupported fake worker message: {msg_type}")
        return 0
    finally:
        close_diarization_store(diarization_store)
        warm_diarizer.close()
        reader.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        emit("error", message=str(error))
        sys.exit(1)
