# SPDX-License-Identifier: GPL-3.0-or-later
"""D11/D11.2: deterministic FIR, Silero and SpeechGate, never platform resampling.

The gate follows cassini-live speech.py/vad.py (769dfe8) state transitions.
Audio and inference live only in the recognition child. Planning retains ranges;
recognition rereads at most one bounded segment, including FIR edge context.
"""

import hashlib
import json
import math
import os
import wave
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

import numpy as np

RATE = 16000
FIR = json.loads(Path(__file__).with_name("fir-48k-16k.json").read_text())
TAPS = np.asarray(FIR["coefficients"], dtype="<f4")


def resampled_chunks(path, start=0, end=None, chunk_size=16000):
    """Seekable FIR blocks implement the published ascending-n float64 sum."""
    with wave.open(str(path), "rb") as source:
        rate, channels = source.getframerate(), source.getnchannels()
        if (
            source.getsampwidth() != 2
            or source.getcomptype() != "NONE"
            or rate not in (16000, 48000)
            or channels not in (1, 2)
        ):
            raise ValueError("Transcription requires PCM16 at 16 or 48 kHz")
        total = source.getnframes()
        count = (total + 2) // 3 if rate == 48000 else total
        end = min(count, count if end is None else end)
        for first in range(start, end, chunk_size):
            last = min(first + chunk_size, end)
            low = max(0, first * 3 - 48) if rate == 48000 else first
            high = min(total, (last - 1) * 3 + 49) if rate == 48000 else last
            source.setpos(low)
            raw = source.readframes(high - low)
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels)
            # Integer PCM values are exact float32. D11.2 averages first.
            samples = samples.mean(axis=1, dtype=np.float32) / np.float32(32768)
            if rate == 16000:
                yield samples
                continue
            yield fir_values(samples, low, first, last)


def fir_values(samples, low, first, last):
    output = np.zeros(last - first, dtype=np.float64)
    positions = np.arange(first, last, dtype=np.int64) * 3 + 48
    for n, coefficient in enumerate(TAPS):
        indices = positions - n - low
        valid = (indices >= 0) & (indices < len(samples))
        output[valid] += float(coefficient) * samples[indices[valid]].astype(np.float64)
    return output.astype("<f4")


def resampled_stream(blocks):
    """The same centred FIR, with <=one block plus 96 context samples resident."""
    samples = np.empty(0, dtype="<f4")
    low = total = first = 0
    for block in blocks:
        if len(block) > 48000:
            raise ValueError("PCM source exceeded its one-second block limit")
        samples = np.concatenate((samples, block))
        total += len(block)
        available = max(0, (total - 49) // 3 + 1)
        while first < available:
            last = min(first + RATE, available)
            yield fir_values(samples, low, first, last)
            first = last
        keep = max(0, first * 3 - 48)
        samples = samples[keep - low :]
        low = keep
    while first < (total + 2) // 3:
        last = min(first + RATE, (total + 2) // 3)
        yield fir_values(samples, low, first, last)
        first = last


def audio_chunks(track):
    if track.get("format") == "opus":
        from .opus_input import pcm_blocks

        with closing(pcm_blocks(track["path"])) as decoded:
            yield from resampled_stream(decoded)
    else:
        yield from resampled_chunks(track["path"])


def audio_segments(track, segments):
    """Second sequential decode pass: retain at most one speech segment, no WAV."""
    if track.get("format") != "opus":
        for segment in segments:
            yield np.concatenate(list(resampled_chunks(track["path"], segment["startSample"], segment["endSample"])))
        return
    with closing(audio_chunks(track)) as source:
        current, cursor = np.empty(0, dtype="<f4"), 0
        for segment in segments:
            start, end = segment["startSample"], segment["endSample"]
            if not 0 < end - start <= 16 * RATE or start < cursor:
                raise ValueError("Invalid or overlapping speech segment")
            pieces = []
            while cursor < end:
                if not len(current):
                    current = next(source, None)
                    if current is None:
                        raise ValueError("Received audio ended before its planned segment")
                take = min(len(current), end - cursor)
                offset = max(0, start - cursor)
                if offset < take:
                    pieces.append(current[offset:take])
                current = current[take:]
                cursor += take
            yield np.concatenate(pieces)


class SpeechDetector:
    frame_size = 512

    def __init__(self, model, session=None):
        if os.environ.get("ORT_DISABLE_TELEMETRY") != "1":
            raise RuntimeError("ORT telemetry must be disabled before import")
        import onnxruntime as ort

        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self.session = session or ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self.runtime = dict(
            version=ort.__version__,
            providers=self.session.get_providers(),
            intraThreads=self.session.get_session_options().intra_op_num_threads,
            interThreads=self.session.get_session_options().inter_op_num_threads,
        )
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def probability(self, frame):
        samples = np.concatenate((self.context, frame.reshape(1, -1)), axis=1)
        output, self.state = self.session.run(
            None, dict(input=samples, state=self.state, sr=np.array(16000, dtype=np.int64))
        )
        self.context = samples[:, -64:].copy()
        return float(output[0, 0])


@dataclass
class SpeechChunk:
    samples: np.ndarray
    start_sample: int
    end: bool = False
    reason: str = "flush"
    pause_cut: int | None = None


class SpeechGate:
    def __init__(self, detector, max_seconds=15):
        self.detector = detector
        self.max_samples = max_seconds * RATE
        self.frame_size = detector.frame_size
        self.tail = np.empty(0, dtype="<f4")
        self.pre = deque(maxlen=5120 // self.frame_size)
        self.votes = deque(maxlen=3)
        self.active = False
        self.quiet = self.duration = self.cursor = 0
        self.buffer = np.empty(0, dtype="<f4")
        self.pause_cut = None
        self.segment_start = self.emitted = 0

    def _chunk(self, samples, end=False, reason="flush"):
        cut = self.pause_cut
        value = SpeechChunk(
            samples,
            self.segment_start + self.emitted,
            end,
            reason,
            cut if cut is not None and 0 < cut < len(samples) else None,
        )
        self.emitted += len(samples)
        if cut is not None:
            self.pause_cut = cut - len(samples) if cut > len(samples) else None
        return value

    def feed(self, samples):
        self.tail = np.concatenate((self.tail, np.asarray(samples, dtype="<f4")))
        chunks = []
        while len(self.tail) >= self.frame_size:
            frame, self.tail = self.tail[: self.frame_size].copy(), self.tail[self.frame_size :]
            self.cursor += self.frame_size
            voiced = self.detector.probability(frame) >= (0.35 if self.active else 0.5)
            if not self.active:
                self.pre.append(frame)
                self.votes.append(voiced)
                if sum(self.votes) < 2:
                    continue
                self.active = True
                self.buffer = np.concatenate(list(self.pre))
                self.duration = len(self.buffer)
                self.segment_start = self.cursor - self.duration
                self.emitted = 0
                self.pre.clear()
                self.votes.clear()
            else:
                self.buffer = np.concatenate((self.buffer, frame))
                self.duration += len(frame)
            self.quiet = 0 if voiced else self.quiet + 1
            quiet_samples = self.quiet * self.frame_size
            if quiet_samples >= 2048:
                self.pause_cut = len(self.buffer) - 1024
            reason = None
            if quiet_samples >= 9600:
                reason = "silence"
            elif self.duration >= self.max_samples - 16000 and quiet_samples >= 2048:
                reason = "durationPause"
            elif self.duration >= self.max_samples:
                reason = "durationLimit"
            while len(self.buffer) > 16000:
                chunks.append(self._chunk(self.buffer[:16000]))
                self.buffer = self.buffer[16000:]
            if reason:
                chunks.append(self._chunk(self.buffer, end=True, reason=reason))
                self.buffer = np.empty(0, dtype="<f4")
                self.active = False
                self.quiet = 0
                self.pause_cut = None
            elif len(self.buffer) >= 16000:
                chunks.append(self._chunk(self.buffer[:16000]))
                self.buffer = self.buffer[16000:]
        return chunks

    def flush(self):
        chunks = []
        if self.active:
            samples = np.concatenate((self.buffer, self.tail))
            if len(samples) > 16000:
                chunks.append(self._chunk(samples[:16000]))
                samples = samples[16000:]
            chunks.append(self._chunk(samples, end=True))
        maximum = self.max_samples
        self.detector.reset()
        self.__init__(self.detector)
        self.max_samples = maximum
        return chunks


def segment_ranges(path, detector, heartbeat=None, *, chunks=None):
    detector.reset()
    gate = SpeechGate(detector)
    start = None
    ranges = []

    def collect(chunks):
        nonlocal start
        for chunk in chunks:
            if start is None:
                start = chunk.start_sample
            if chunk.end:
                ranges.append(
                    dict(startSample=start, endSample=chunk.start_sample + len(chunk.samples), reason=chunk.reason)
                )
                start = None

    import time

    last_notice = time.monotonic()
    for samples in resampled_chunks(path) if chunks is None else chunks:
        collect(gate.feed(samples))
        if heartbeat is not None and time.monotonic() - last_notice >= 1:
            heartbeat()
            last_notice = time.monotonic()
    collect(gate.flush())
    return ranges


def recognise_segment(model, path, segment, offset_ms=0, *, samples=None):
    if samples is None:
        samples = np.concatenate(list(resampled_chunks(path, segment["startSample"], segment["endSample"])))
    input_hash = hashlib.sha256(samples.tobytes()).hexdigest()
    result = model.transcribe(samples, language=None, word_timestamps=True)
    base = math.floor(offset_ms) + segment["startSample"] * 1000 // 16000
    words = []
    for word in result.get("words", []):
        words.append(
            dict(
                startMs=base + round(word["start"] * 1000),
                endMs=base + round(word["end"] * 1000),
                text=word.get("word", word.get("text", "")),
            )
        )
    return dict(segment, text=result["text"]), words, result.get("language") or "und", input_hash


def process_tracks(model, tracks, vad_model, notify):
    """One complete queue job; all inference and floating PCM stay in the child."""
    import time

    detector = SpeechDetector(vad_model)
    plan = []
    diagnostics = []
    for order, track in enumerate(tracks):
        path = Path(track["path"])
        offset = track.get("offsetMs")
        if offset is None:
            offset = 0
            diagnostics.append(dict(code="unknown-offset-display-zero", track=track["id"]))
        if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
            raise ValueError("Invalid capture offset")
        for segment in segment_ranges(
            path, detector, lambda track=track: notify("planning", trackId=track["id"]), chunks=audio_chunks(track)
        ):
            plan.append((order, track, segment, offset))
    remaining = sum((s["endSample"] - s["startSample"]) / RATE for _, _, s, _ in plan)
    notify("planned", etaMinutes=math.ceil(remaining * 0.15 / 60), segments=len(plan), diagnostics=diagnostics)
    elapsed = processed = 0.0
    language = "und"
    for _order, grouped in groupby(plan, key=lambda entry: entry[0]):
        entries = list(grouped)
        track = entries[0][1]
        with closing(audio_segments(track, [entry[2] for entry in entries])) as source:
            for (order, track, segment, offset), samples in zip(entries, source, strict=True):
                started = time.monotonic()
                value, words, language, input_hash = recognise_segment(
                    model, track["path"], segment, offset, samples=samples
                )
                elapsed += time.monotonic() - started
                seconds = (segment["endSample"] - segment["startSample"]) / RATE
                processed += seconds
                remaining = max(0, remaining - seconds)
                factor = elapsed / processed if processed else 0.15
                notify(
                    "progress",
                    inputSha256=input_hash,
                    trackId=track["id"],
                    trackOrder=order,
                    segment=value,
                    words=words,
                    language=language,
                    etaMinutes=math.ceil(remaining * factor / 60),
                    realtimeFactor=factor,
                )
    return dict(language=language, diagnostics=diagnostics, segments=len(plan), vad=detector.runtime)
