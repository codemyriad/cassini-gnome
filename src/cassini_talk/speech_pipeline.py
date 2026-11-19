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
from bisect import bisect_right
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from itertools import groupby
from pathlib import Path

import numpy as np

from . import placement
from .recognition_cache import (
    RECOGNITION_CACHE_FORMAT,
    cache_entry_payload,
    derive_segment_cache_key,
    is_valid_model_identity,
    normalize_language,
    raw_seconds_to_relative_words,
    read_recognition_cache_file,
    valid_cached_entry,
)

RATE = 16000
PREFIX_BLOCK_SAMPLES = 8192  # 16 exact 512-sample Silero/ActivityMeter frames (512 ms)
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
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            # Integer PCM values are exact float32. D11.2 averages stereo first;
            # mono already has one value per frame. Both buffers are owned here.
            if channels == 2:
                samples = samples.reshape(-1, channels).mean(axis=1, dtype=np.float32)
            samples /= np.float32(32768)
            if rate == 16000:
                yield samples
                continue
            yield fir_values(samples, low, first, last)


def fir_values(samples, low, first, last):
    # Convert once; every tap reads a strided view of these same float64 values.
    samples = np.asarray(samples, dtype=np.float64)
    output = np.zeros(last - first, dtype=np.float64)
    for n, coefficient in enumerate(TAPS):
        index = first * 3 + 48 - n - low
        begin = max(0, (-index + 2) // 3)
        end = min(len(output), (len(samples) - index + 2) // 3)
        if begin < end:
            # The same per-tap multiply/add order, without materializing an
            # index array and two boolean gathers for every one of the 97 taps.
            values = samples[index + begin * 3 : index + end * 3 : 3]
            output[begin:end] += float(coefficient) * values
    return output.astype("<f4")


class IncrementalResampler:
    """Stateful centred 97-tap 48 kHz -> 16 kHz FIR keeping <=1 block + 96 context samples."""

    def __init__(self):
        self.samples = np.empty(0, dtype="<f4")
        self.low = 0
        self.total = 0
        self.first = 0

    def feed(self, block):
        block = np.asarray(block, dtype="<f4")
        if not len(block):
            return []
        if len(block) > 48000:
            raise ValueError("PCM source exceeded its one-second block limit")
        self.samples = np.concatenate((self.samples, block))
        self.total += len(block)
        available = max(0, (self.total - 49) // 3 + 1)
        out = []
        while self.first < available:
            last = min(self.first + RATE, available)
            out.append(fir_values(self.samples, self.low, self.first, last))
            self.first = last
        keep = max(0, self.first * 3 - 48)
        self.samples = self.samples[keep - self.low :]
        self.low = keep
        return out

    def flush(self):
        out = []
        target = (self.total + 2) // 3
        while self.first < target:
            last = min(self.first + RATE, target)
            out.append(fir_values(self.samples, self.low, self.first, last))
            self.first = last
        self.samples = np.empty(0, dtype="<f4")
        self.low = self.total
        return out


def resampled_stream(blocks):
    """The same centred FIR, with <=one block plus 96 context samples resident."""
    resampler = IncrementalResampler()
    for block in blocks:
        yield from resampler.feed(block)
    yield from resampler.flush()


def streamed(track):
    """Received Opus files and compacted capture spans (S-12 §5) decode sequentially."""
    from .mapped_audio import required

    return (
        "ownershipWindows" in track or track.get("format") == "opus" or track.get("codec") == "opus" or required(track)
    )


def audio_chunks(track):
    from .source_ownership import FrameMask

    # Windows are cut on the session's 16 kHz grid, then moved by the track's whole-sample
    # D55 placement: the sample the word times and the rejoin put the track's first sample at.
    mask = FrameMask(track.get("ownershipWindows"), RATE, placement.frame(track.get("offsetMs") or 0, RATE) / 16)
    cursor = 0
    with closing(_unmasked_chunks(track)) as source:
        for block in source:
            sections = mask.sections(cursor, len(block))
            if sections != [(0, len(block))]:
                selected = np.zeros_like(block)
                for left, right in sections:
                    selected[left:right] = block[left:right]
                block = selected
            yield block
            cursor += len(block)


def _unmasked_chunks(track):
    from .mapped_audio import chunks, required

    if required(track):
        yield from chunks(track)
    elif track.get("codec") == "opus":
        from .track_source import mono_blocks

        with closing(mono_blocks(track["path"])) as decoded:
            yield from resampled_stream(decoded)
    elif track.get("format") == "opus":
        from .opus_input import pcm_blocks

        with closing(pcm_blocks(track["path"])) as decoded:
            yield from resampled_stream(decoded)
    else:
        yield from resampled_chunks(track["path"])


def audio_segments(track, segments, heartbeat=None, *, clock=None):
    """Second sequential decode pass: retain at most one speech segment, no WAV."""
    if not streamed(track):
        for segment in segments:
            yield np.concatenate(list(resampled_chunks(track["path"], segment["startSample"], segment["endSample"])))
        return
    import time

    clock = clock or time.monotonic
    last_notice = clock()
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
                if heartbeat is not None and clock() - last_notice >= 1:
                    heartbeat()
                    last_notice = clock()
                take = min(len(current), end - cursor)
                offset = max(0, start - cursor)
                if offset < take:
                    pieces.append(current[offset:take])
                current = current[take:]
                cursor += take
            yield np.concatenate(pieces)


class SpeechDetector:
    frame_size = 512
    input_stage = "silero-input-hpf-butter2-100hz-v1"

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
            inputStage=self.input_stage,
            version=ort.__version__,
            providers=self.session.get_providers(),
            intraThreads=self.session.get_session_options().intra_op_num_threads,
            interThreads=self.session.get_session_options().inter_op_num_threads,
        )
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
        # D26.1: WebRTC APM's capture high-pass, for Silero only. Preserve
        # binary64 operation order; retain state through every frame in a track.
        # The gate and every recogniser/diarizer/voiceprint still receive raw PCM.
        x1, x2, y1, y2 = self.hpf
        filtered = np.empty(len(frame), dtype="<f4")
        for index, value in enumerate(frame):
            x = float(value)
            y = (
                0.9726138984998437 * x
                + -1.9452277969996874 * x1
                + 0.9726138984998437 * x2
                - -1.9444776577670935 * y1
                - 0.9459779362322813 * y2
            )
            if -1e-25 < y < 1e-25:
                y = 0.0
            x2, x1, y2, y1 = x1, x, y1, y
            filtered[index] = y
        self.hpf = (x1, x2, y1, y2)
        samples = np.concatenate((self.context, filtered.reshape(1, -1)), axis=1)
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

    def snapshot(self):
        return (
            self.tail.copy(),
            tuple(frame.copy() for frame in self.pre),
            tuple(self.votes),
            self.active,
            self.quiet,
            self.duration,
            self.cursor,
            self.buffer.copy(),
            self.pause_cut,
            self.segment_start,
            self.emitted,
            self.max_samples,
        )

    def restore(self, snap):
        (
            tail,
            pre,
            votes,
            active,
            quiet,
            duration,
            cursor,
            buffer,
            pause_cut,
            segment_start,
            emitted,
            max_samples,
        ) = snap
        self.tail = tail.copy()
        self.pre = deque((frame.copy() for frame in pre), maxlen=5120 // self.frame_size)
        self.votes = deque(votes, maxlen=3)
        self.active = active
        self.quiet = quiet
        self.duration = duration
        self.cursor = cursor
        self.buffer = buffer.copy()
        self.pause_cut = pause_cut
        self.segment_start = segment_start
        self.emitted = emitted
        self.max_samples = max_samples

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


@dataclass(frozen=True)
class SegmentationPrefix:
    """Block-verified 16 kHz PCM prefix + VAD/SpeechGate/ActivityMeter checkpoint."""

    samples: int
    block_hashes: tuple[bytes, ...]
    ownership_windows: tuple | None
    offset_ms: int | float
    vad_input_stage: str
    max_samples: int
    detector_state: tuple
    gate_state: tuple
    meter_codes: bytes
    closed_ranges: tuple[dict, ...]
    open_start: int | None


def ownership_prefix_compatible(prefix: SegmentationPrefix, final_ownership, final_offset) -> bool:
    """Check whether final ownershipWindows makes zero retroactive cuts inside [0, prefix.samples)."""
    from .source_ownership import FrameMask

    if prefix.samples <= 0:
        return False
    try:
        before = FrameMask(prefix.ownership_windows, RATE, placement.frame(prefix.offset_ms, RATE) / 16)
        after = FrameMask(final_ownership, RATE, placement.frame(final_offset, RATE) / 16)
    except ValueError:
        return False
    return before.sections(0, prefix.samples) == after.sections(0, prefix.samples)


def _lookup_prefix(prefix_store, track):
    if not prefix_store:
        return None
    path = Path(track["path"])
    session_dir = Path(track["sessionDirectory"]) if "sessionDirectory" in track else path.parent
    try:
        resolved = str(session_dir.resolve())
    except OSError:
        resolved = str(session_dir.absolute())
    candidates = []
    if track.get("captureTrackId") is not None:
        candidates.append((resolved, track["captureTrackId"]))
    candidates.append((resolved, track["id"]))
    candidates.append((resolved, path.name))
    for key in candidates:
        found = prefix_store.get(key)
        if isinstance(found, SegmentationPrefix):
            return found
    return None


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


def segment_ranges_with_prefix(track, detector, meter, offset, heartbeat=None, *, prefix=None):
    """Plan segments, reusing a cryptographically verified SegmentationPrefix when exact.

    Verifies every PREFIX_BLOCK_SAMPLES block of audio_chunks(track) over
    [0, prefix.samples) via SHA-256 and checks ownershipWindows compatibility.
    On any mismatch or missing snapshot capability, falls back cleanly to full
    segment_ranges. Returns (ranges, prefix_reused).
    """
    import time

    path = Path(track["path"])
    can_try = (
        isinstance(prefix, SegmentationPrefix)
        and prefix.samples > 0
        and prefix.samples == len(prefix.block_hashes) * PREFIX_BLOCK_SAMPLES
        and hasattr(detector, "restore")
        and getattr(detector, "input_stage", None) == prefix.vad_input_stage
        and prefix.max_samples == SpeechGate(detector).max_samples
        and ownership_prefix_compatible(prefix, track.get("ownershipWindows"), offset)
    )
    if not can_try:
        return (
            segment_ranges(path, detector, heartbeat, chunks=meter.wrap(audio_chunks(track))),
            False,
        )

    last_notice = time.monotonic()
    matched = True
    consumed = 0
    block_index = 0
    pending = np.empty(0, dtype="<f4")
    leftover = np.empty(0, dtype="<f4")
    with closing(audio_chunks(track)) as source:
        for block in source:
            block = np.asarray(block, dtype="<f4")
            if heartbeat is not None and time.monotonic() - last_notice >= 1:
                heartbeat()
                last_notice = time.monotonic()
            if not len(block):
                continue
            pending = np.concatenate((pending, block)) if len(pending) else block
            while len(pending) >= PREFIX_BLOCK_SAMPLES and block_index < len(prefix.block_hashes):
                piece = pending[:PREFIX_BLOCK_SAMPLES]
                pending = pending[PREFIX_BLOCK_SAMPLES:]
                if hashlib.sha256(piece.tobytes()).digest() != prefix.block_hashes[block_index]:
                    matched = False
                    break
                consumed += PREFIX_BLOCK_SAMPLES
                block_index += 1
            if not matched:
                break
            if block_index == len(prefix.block_hashes):
                leftover = pending
                pending = np.empty(0, dtype="<f4")
                break
        if not matched or block_index != len(prefix.block_hashes) or consumed != prefix.samples:
            meter.codes.clear()
            meter.samples = 0
            meter._tail = np.empty(0, dtype="<f4")
            meter._inside = []
            return (
                segment_ranges(path, detector, heartbeat, chunks=meter.wrap(audio_chunks(track))),
                False,
            )

        # Prefix matched bit-for-bit: restore VAD/gate/meter state and gate only the tail.
        detector.restore(prefix.detector_state)
        gate = SpeechGate(detector)
        gate.restore(prefix.gate_state)
        meter.codes = bytearray(prefix.meter_codes)
        meter.samples = prefix.samples
        meter._tail = np.empty(0, dtype="<f4")
        meter._inside = []
        ranges = [dict(item) for item in prefix.closed_ranges]
        start = prefix.open_start

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

        if len(leftover):
            meter._feed(leftover)
            collect(gate.feed(leftover))
        for block in source:
            block = np.asarray(block, dtype="<f4")
            if len(block):
                meter._feed(block)
                collect(gate.feed(block))
            if heartbeat is not None and time.monotonic() - last_notice >= 1:
                heartbeat()
                last_notice = time.monotonic()
        collect(gate.flush())
        return ranges, True


def read_recognition_cache(options):
    if not isinstance(options, dict):
        return {}
    path, model = options.get("path"), options.get("model")
    if not isinstance(path, str) or not path or not is_valid_model_identity(model):
        return {}
    return read_recognition_cache_file(path, model)


def place_and_clamp_words(
    segment,
    offset_ms,
    sample_count,
    raw_text,
    relative_words,
    language,
    *,
    audio_end_ms=None,
    presence=None,
    presence_starts=None,
):
    """Apply D55 placement, G11 clamping, and D63 #3 presence filtering once at finalization."""
    base = placement.word_ms(offset_ms, segment["startSample"])
    last_sample = min(segment["endSample"], segment["startSample"] + sample_count)
    end = base + (last_sample - segment["startSample"]) * 1000 // RATE
    if audio_end_ms is not None:
        end = min(end, audio_end_ms)
    words = []
    for word in relative_words:
        start = min(end, max(base, base + word["startMs"]))
        words.append(
            dict(
                startMs=start,
                endMs=max(start, min(end, base + word["endMs"])),
                text=word["text"],
            )
        )
    kept = present_words(words, presence, starts=presence_starts)
    if len(kept) != len(words):
        words = kept
        final_text = " ".join(word["text"] for word in kept)
    else:
        final_text = raw_text
    return dict(segment, text=final_text), words, language


def raw_result_to_cache_entry(model_identity, samples, result):
    """Build a validated cassini.recognition-cache.v2 entry from raw recognizer output."""
    samples = np.asarray(samples, dtype="<f4")
    if not len(samples) or not np.isfinite(samples).all() or not is_valid_model_identity(model_identity):
        return None, None
    input_hash = hashlib.sha256(samples.tobytes()).hexdigest()
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        return None, input_hash
    relative_words = raw_seconds_to_relative_words(result.get("words", []))
    if relative_words is None:
        return None, input_hash
    language = normalize_language(result.get("language"))
    try:
        key = derive_segment_cache_key(model_identity, input_hash, len(samples))
    except ValueError:
        return None, input_hash
    entry = dict(sampleCount=len(samples), text=result["text"], language=language, relativeWords=relative_words)
    if not valid_cached_entry(key, entry, model=model_identity, pcm_sha256=input_hash, sample_count=len(samples)):
        return None, input_hash
    return dict(key=key, **entry), input_hash


def recognise_segment(model, path, segment, offset_ms=0, *, samples=None, audio_end_ms=None, input_hash=None):
    if samples is None:
        samples = np.concatenate(list(resampled_chunks(path, segment["startSample"], segment["endSample"])))
    samples = np.asarray(samples, dtype="<f4")
    if input_hash is None:
        input_hash = hashlib.sha256(samples.tobytes()).hexdigest()
    result = model.transcribe(samples, language=None, word_timestamps=True)
    # D55: session ms of the segment's first 16 kHz sample, from the one placement. G11: a
    # word ends within the PCM it was recognised from, counted in whole ms from that first
    # sample (Mac's MeetingTranscriber.clamped: start + count * 1000 / 16000).
    base = placement.word_ms(offset_ms, segment["startSample"])
    last_sample = min(segment["endSample"], segment["startSample"] + len(samples))
    end = base + (last_sample - segment["startSample"]) * 1000 // RATE
    if audio_end_ms is not None:
        end = min(end, audio_end_ms)
    words = []
    for word in result.get("words", []):
        # A recognizer can report its last word beyond the supplied PCM. Keep
        # the text/count, but never assign it time outside its own source.
        start = min(end, max(base, base + round(word["start"] * 1000)))
        words.append(
            dict(
                startMs=start,
                endMs=max(start, min(end, base + round(word["end"] * 1000))),
                text=word.get("word", word.get("text", "")),
            )
        )
    return dict(segment, text=result["text"]), words, result.get("language") or "und", input_hash


def present_words(words, presence, *, starts=None):
    """D63 #3: the words whose midpoint lies in one of the track's presence ranges.

    `presence` holds merged, sorted 16 kHz [start, end) ranges from the session
    origin, where a word's midpoint is exactly 8 * (startMs + endMs). None, a
    track that is not placed, keeps every word.
    """
    if presence is None:
        return words
    if starts is None:
        starts = [start for start, _end in presence]
    kept = []
    for word in words:
        middle = 8 * (word["startMs"] + word["endMs"])
        index = bisect_right(starts, middle) - 1
        if index >= 0 and middle < presence[index][1]:
            kept.append(word)
    return kept


def _source_end(track):
    from .mapped_audio import end_ms, required

    if required(track):
        return end_ms(track)
    if (
        track.get("codec") != "opus"
        and type(track.get("frames")) is int
        and track["frames"] > 0
        and track.get("sampleRate")
    ):
        # Opus frames are source metadata; its decoded PCM, which is also
        # what publication uses, already bounds each segment's words. D55: the
        # track's 16 kHz samples end at its placement plus their count, in the
        # session ms of the word times; never earlier than a segment's own end.
        count = -(-track["frames"] * RATE // track["sampleRate"])
        return placement.word_ms(track.get("offsetMs") or 0, count)
    return None


def placed_offset(track, diagnostics):
    """The offset the write-down places a track's words at (D55 M9).

    An unknown offset is placed at an assumed 0 and reported in `diagnostics`;
    the parent flags the attempt (writingDown.offsetsAssumedZero).
    """
    offset = track.get("offsetMs")
    if offset is None:
        offset = 0
        diagnostics.append(dict(code="unknown-offset-display-zero", track=track["id"]))
    if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
        raise ValueError("Invalid capture offset")
    return offset


def process_tracks(
    model,
    tracks,
    vad_model,
    notify,
    *,
    extra_seconds=0,
    recognition_cache=None,
    detector=None,
    prefix_store=None,
):
    """One complete queue job; all inference and floating PCM stay in the child."""
    import time

    from . import recognition_fallback as fallback

    cache_model = recognition_cache.get("model") if isinstance(recognition_cache, dict) else None
    cache_enabled = (
        isinstance(recognition_cache, dict)
        and isinstance(recognition_cache.get("path"), str)
        and bool(recognition_cache["path"])
        and is_valid_model_identity(cache_model)
    )
    cached_segments = read_recognition_cache(recognition_cache) if cache_enabled else {}
    detector = detector if detector is not None else SpeechDetector(vad_model)
    plan = []
    meters = {}
    diagnostics = []
    decided = {}  # R71/D52.4 provenance by track order: the tracks of each firing speaker
    prefixes_reused = 0
    # D56: the parent measured these; they are never decoded, planned or recognised.
    audible = [(order, track) for order, track in enumerate(tracks) if track.get("silent") is not True]
    for order, track in audible:
        offset = placed_offset(track, diagnostics)
        meter = fallback.ActivityMeter(fallback.track_ranges(track.get("sessionPresence"), offset))
        meters[order] = meter
        prefix = _lookup_prefix(prefix_store, track)
        segments, reused = segment_ranges_with_prefix(
            track,
            detector,
            meter,
            offset,
            lambda track=track: notify("planning", trackId=track["id"]),
            prefix=prefix,
        )
        if reused:
            prefixes_reused += 1
        for segment in segments:
            plan.append((order, track, segment, offset))
    remaining = sum((s["endSample"] - s["startSample"]) / RATE for _, _, s, _ in plan)
    initial_factor = getattr(model, "initial_realtime_factor", 0.15)
    notify(
        "planned",
        etaMinutes=math.ceil((remaining * initial_factor + extra_seconds) / 60),
        segments=len(plan),
        diagnostics=diagnostics,
        prefixesReused=prefixes_reused,
    )
    elapsed = processed = 0.0
    language = "und"

    def eta():
        factor = elapsed / processed if processed else initial_factor
        return math.ceil((remaining * factor + extra_seconds) / 60)

    def emit(event):
        # Only events actually sent decide the summary language, never a
        # discarded fallback window or a replaced VAD segment.
        nonlocal language
        language = event["language"]
        # A held event goes out after later tracks: its ETA is the one when sent.
        notify("progress", **dict(event, etaMinutes=eta()))

    presence_starts = {
        order: [start for start, _end in p]
        for order, track in audible
        if (p := track.get("sessionPresence")) is not None
    }

    def recognise(order, track, segment, offset, samples, audio_end):
        nonlocal elapsed, processed, remaining
        samples = np.asarray(samples, dtype="<f4")
        input_hash = hashlib.sha256(samples.tobytes()).hexdigest()
        presence = track.get("sessionPresence")
        starts = presence_starts.get(order)
        finite = bool(len(samples) and np.isfinite(samples).all())
        cache_key = (
            derive_segment_cache_key(cache_model, input_hash, len(samples)) if (cache_enabled and finite) else None
        )
        cached = cached_segments.get(cache_key) if cache_key is not None else None
        if cached is not None and not valid_cached_entry(
            cache_key, cached, model=cache_model, pcm_sha256=input_hash, sample_count=len(samples)
        ):
            cached = None
        seconds = (segment["endSample"] - segment["startSample"]) / RATE
        if cached is not None:
            value, words, detected = place_and_clamp_words(
                segment,
                offset,
                len(samples),
                cached["text"],
                cached["relativeWords"],
                cached["language"],
                audio_end_ms=audio_end,
                presence=presence,
                presence_starts=starts,
            )
            remaining = max(0, remaining - seconds)
            factor = elapsed / processed if processed else initial_factor
        else:
            started = time.monotonic()
            result = model.transcribe(samples, language=None, word_timestamps=True)
            cache_entry, _ = raw_result_to_cache_entry(cache_model, samples, result) if cache_enabled else (None, None)
            if cache_entry is not None:
                value, words, detected = place_and_clamp_words(
                    segment,
                    offset,
                    len(samples),
                    cache_entry["text"],
                    cache_entry["relativeWords"],
                    cache_entry["language"],
                    audio_end_ms=audio_end,
                    presence=presence,
                    presence_starts=starts,
                )
            else:
                # Non-cacheable raw output still uses the exact existing placement/clamp path.
                base = placement.word_ms(offset, segment["startSample"])
                last_sample = min(segment["endSample"], segment["startSample"] + len(samples))
                end = base + (last_sample - segment["startSample"]) * 1000 // RATE
                if audio_end is not None:
                    end = min(end, audio_end)
                raw_words = []
                for word in result.get("words", []):
                    start = min(end, max(base, base + round(word["start"] * 1000)))
                    raw_words.append(
                        dict(
                            startMs=start,
                            endMs=max(start, min(end, base + round(word["end"] * 1000))),
                            text=word.get("word", word.get("text", "")),
                        )
                    )
                kept = present_words(raw_words, presence, starts=starts)
                if len(kept) != len(raw_words):
                    words = kept
                    final_text = " ".join(word["text"] for word in kept)
                else:
                    words = raw_words
                    final_text = result["text"]
                value = dict(segment, text=final_text)
                detected = result.get("language") or "und"
            elapsed += time.monotonic() - started
            processed += seconds
            remaining = max(0, remaining - seconds)
            factor = elapsed / processed if processed else initial_factor
            if cache_key is not None and cache_entry is not None:
                cached_segments[cache_key] = cache_entry_payload(cache_entry)
                notify("planning", trackId=track["id"], cacheEntry=cache_entry)
        return dict(
            inputSha256=input_hash,
            trackId=track["id"],
            trackOrder=order,
            segment=value,
            words=words,
            language=detected,
            etaMinutes=eta(),
            realtimeFactor=factor,
        )

    by_order = {order: list(entries) for order, entries in groupby(plan, key=lambda entry: entry[0])}
    # D63 #1: the 15 s and 10-word tests pool every audible track of one speaker
    # id. Activity is still measured, and windows laid, per track in its presence.
    actives = {order: meters[order].active() for order, _track in audible}
    # A job without a stored speaker id is a speaker of its own.
    speakers = {order: track.get("speaker", track["id"]) for order, track in audible}
    facts = {
        order: dict(
            trackId=track.get("captureTrackId", track["id"]),
            speaker=speakers[order],
            activeFrames=sum(actives[order]),
            vadWords=0,
            fallbackWords=0,
        )
        for order, track in audible
    }
    frames = {speaker: value[0] for speaker, value in fallback.pooled(facts.values()).items()}
    spoken = dict.fromkeys(frames, 0)
    last = {speaker: order for order, speaker in speakers.items()}
    # A speaker with plenty of active audio holds its VAD events, across all its
    # tracks, until together they reach FEW_WORDS; others stream exactly as before.
    held = {speaker: [] for speaker, count in frames.items() if fallback.holds(count)}
    context = {}

    def heartbeat(track):
        notify("planning", trackId=track["id"])

    def windowed(order, track):
        """The VAD-free windows of one track and the words they keep."""
        nonlocal remaining
        offset, audio_end = context[order]
        windows = fallback.active_windows(meters[order].samples, actives[order], meters[order].presence)
        remaining += sum(end - start for start, end in windows) / RATE
        events, words = [], []
        with closing(audio_chunks(track)) as chunks:
            beat = partial(heartbeat, track)
            for (start, end), samples in zip(windows, fallback.audio_windows(chunks, windows, beat), strict=True):
                segment = dict(startSample=start, endSample=end, reason="noVadWindow")
                event = recognise(order, track, segment, offset, samples, audio_end)
                heartbeat(track)
                base = placement.word_ms(offset, start)
                kept = fallback.energetic(event["words"], samples, base)
                previous = events[-1]["segment"]["endSample"] if events else None
                if previous is not None and start < previous:
                    words = fallback.merge_seam(words, kept, base, (previous - start) * 1000 // RATE)
                else:
                    words += kept
                events.append(event)
        return windows, events, words

    def emit_fallback(offset, events, words):
        # Keep words in global time order across events: each word goes to the
        # last window starting at or before it, not to the window it came from.
        words = sorted(words, key=lambda word: word["startMs"])
        starts = [placement.word_ms(offset, event["segment"]["startSample"]) for event in events]
        owned = [[] for _ in events]
        for word in words:
            owned[max(0, bisect_right(starts, word["startMs"]) - 1)].append(word)
        for event, kept in zip(events, owned, strict=True):
            event["words"] = kept
            event["segment"] = dict(event["segment"], text=" ".join(word["text"] for word in kept))
            emit(event)

    for order, track in audible:
        speaker = speakers[order]
        entries = by_order.get(order, [])
        meter = meters[order]
        offset = entries[0][3] if entries else track.get("offsetMs") or 0
        context[order] = offset, _source_end(track)
        with closing(audio_segments(track, [entry[2] for entry in entries], partial(heartbeat, track))) as source:
            for (order, track, segment, offset), samples in zip(entries, source, strict=True):
                event = recognise(order, track, segment, offset, samples, context[order][1])
                facts[order]["vadWords"] += len(event["words"])
                spoken[speaker] += len(event["words"])
                if speaker not in held:
                    emit(event)
                    continue
                held[speaker].append(event)
                # Held events send nothing; keep the parent's reply deadline alive.
                heartbeat(track)
                if not fallback.fires(frames[speaker], spoken[speaker]):
                    for event in held.pop(speaker):
                        emit(event)
        if facts[order]["vadWords"] == 0 and meter.samples >= 5 * RATE:
            floor, threshold = meter.threshold_db()
            diagnostics.append(
                dict(
                    code="no-words-breakdown",
                    track=track["id"],
                    audioSeconds=meter.samples / RATE,
                    activeSeconds=fallback.seconds(facts[order]["activeFrames"]),
                    floorDb=floor,
                    vadSegments=len(entries),
                    vadSpeechSeconds=sum((e[2]["endSample"] - e[2]["startSample"]) / RATE for e in entries),
                )
            )
        if speaker not in held or order != last[speaker]:
            continue
        # The speaker's last track is recognised and it still has too few words:
        # each of its tracks runs its own windows and keeps its own margin.
        group = [(member, value) for member, value in audible if speakers[member] == speaker]
        passes = {}
        for member, member_track in group:
            passes[member] = windowed(member, member_track)
            facts[member]["fallbackWords"] = len(passes[member][2])
        _verdict, rows = fallback.decide([facts[member] for member, _track in group])
        events = held.pop(speaker)
        for (member, member_track), row in zip(group, rows, strict=True):
            windows, replacements, words = passes[member]
            floor, threshold = meters[member].threshold_db()
            diagnostics.append(
                dict(
                    code="few-words-fallback",
                    track=member_track["id"],
                    trackOrder=member,
                    activeSeconds=row["activeSeconds"],
                    personActiveSeconds=row["personActiveSeconds"],
                    floorDb=floor,
                    thresholdDb=threshold,
                    vadSegments=len(by_order.get(member, [])),
                    vadWords=row["vadWords"],
                    windows=len(windows),
                    windowSeconds=sum(end - start for start, end in windows) / RATE,
                    fallbackWords=row["fallbackWords"],
                    need=fallback.need(row["vadWords"]),
                    used=row["used"],
                )
            )
            decided[member] = row
            if row["used"]:
                emit_fallback(context[member][0], replacements, words)
            else:
                for event in events:
                    if event["trackOrder"] == member:
                        emit(event)
    result = dict(language=language, diagnostics=diagnostics, segments=len(plan), vad=detector.runtime)
    if decided:
        windows = dict(windowMs=fallback.WINDOW * 1000 // RATE, overlapMs=fallback.OVERLAP * 1000 // RATE)
        result["fallback"] = dict(method="vadFreeWindows", **windows, tracks=[decided[o] for o in sorted(decided)])
    return result
