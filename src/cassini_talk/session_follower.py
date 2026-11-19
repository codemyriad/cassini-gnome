# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only session-following precompute worker for D-1121 (D109 / D-1117).

Operates strictly outside audio capture: opens session.json and track-*.wav
read-only (O_RDONLY | O_NOFOLLOW) inside the background_worker_priority()
speech child, carries exact S-12 clock mapping / FIR / Silero VAD / SpeechGate
state across consecutive fixed intervals and span rotations, schedules
simultaneous tracks fairly in earliest-session-time order with bounded RAM,
and produces cassini.recognition-cache.v2 entries plus verified
SegmentationPrefix checkpoints for warm Stop handoff.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import struct
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import placement
from .library_files import regular
from .recognition_cache import is_valid_model_identity
from .recording_session import KINDS, METADATA_LIMIT, strict_json
from .source_ownership import FrameMask
from .source_ownership import windows as ownership_windows

RATE_16K = 16000
RATE_48K = 48000
RIGHT_HALO_48K = 49
QUANTUM_16K = 8192  # 16 exact 512-sample Silero frames (512 ms)
QUANTUM_48K = QUANTUM_16K * 3  # 24576 samples (512 ms at 48 kHz)
MAX_REQUEST_BYTES = 512 * 1024


class StdinLineReader:
    """Unbuffered fd line reader compatible with selectors, with fallback for in-memory test streams."""

    def __init__(self, source):
        import io

        self.fd: int | None = None
        self._stream = None
        self._buf = bytearray()
        self._eof = False
        self._selector = None
        if isinstance(source, int):
            self.fd = source
        else:
            try:
                self.fd = source.fileno()
            except (AttributeError, OSError, io.UnsupportedOperation):
                self._stream = getattr(source, "buffer", source)
        if self.fd is not None:
            self._selector = selectors.DefaultSelector()
            self._selector.register(self.fd, selectors.EVENT_READ)

    def _pop_line(self, max_bytes: int) -> bytes | None:
        newline = self._buf.find(b"\n")
        if newline < 0:
            if len(self._buf) > max_bytes:
                raise ValueError("Worker request exceeds limit")
            return None
        if newline + 1 > max_bytes:
            raise ValueError("Worker request exceeds limit")
        line = bytes(self._buf[: newline + 1])
        del self._buf[: newline + 1]
        return line

    def try_readline(self, timeout: float = 0.0, *, max_bytes: int = MAX_REQUEST_BYTES) -> bytes | None:
        """Return one newline-terminated line, b'' on EOF, or None on timeout."""
        if self._stream is not None:
            raw = self._stream.readline(max_bytes + 1)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if len(raw) > max_bytes:
                raise ValueError("Worker request exceeds limit")
            return raw
        line = self._pop_line(max_bytes)
        if line is not None:
            return line
        if self._eof:
            if self._buf:
                if len(self._buf) > max_bytes:
                    raise ValueError("Worker request exceeds limit")
                rest = bytes(self._buf)
                self._buf.clear()
                return rest
            return b""
        events = self._selector.select(max(0.0, timeout))
        if not events:
            return None
        while True:
            try:
                chunk = os.read(self.fd, 65536)
                break
            except InterruptedError:
                continue
        if not chunk:
            self._eof = True
            if self._buf:
                if len(self._buf) > max_bytes:
                    raise ValueError("Worker request exceeds limit")
                rest = bytes(self._buf)
                self._buf.clear()
                return rest
            return b""
        self._buf.extend(chunk)
        return self._pop_line(max_bytes)

    def readline(self, *, max_bytes: int = MAX_REQUEST_BYTES) -> bytes:
        while True:
            line = self.try_readline(timeout=0.5, max_bytes=max_bytes)
            if line is not None:
                return line

    def close(self) -> None:
        if self._selector is not None:
            self._selector.close()
            self._selector = None


def _open_session_dir_ro(directory: Path) -> int:
    return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def read_session_readonly(directory: Path) -> dict | None:
    """Read session.json strictly read-only without following symlinks."""
    try:
        dir_fd = _open_session_dir_ro(directory)
    except OSError:
        return None
    try:
        with regular(dir_fd, "session.json") as (stream, _info):
            raw = stream.read(METADATA_LIMIT + 1)
    except (OSError, ValueError):
        return None
    finally:
        os.close(dir_fd)
    if len(raw) > METADATA_LIMIT:
        return None
    try:
        value = strict_json(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def read_wav_committed(dir_fd: int, filename: str) -> tuple[int, int, int] | None:
    """Return (committed_frames, rate, channels) bounded by both WAV header and st_size."""
    try:
        with regular(dir_fd, filename) as (stream, info):
            header = stream.read(44)
    except (OSError, ValueError):
        return None
    if len(header) != 44:
        return None
    riff, _, wave_tag, fmt, size, encoding, channels, rate, byte_rate, alignment, bits, data_tag, data_size = (
        struct.unpack("<4sI4s4sIHHIIHH4sI", header)
    )
    if (
        riff != b"RIFF"
        or wave_tag != b"WAVE"
        or fmt != b"fmt "
        or data_tag != b"data"
        or size != 16
        or encoding != 1
        or bits != 16
        or channels not in (1, 2)
        or not 8000 <= rate <= 192000
        or alignment != channels * 2
        or byte_rate != rate * channels * 2
        or info.st_size < 44
    ):
        return None
    disk_frames = (info.st_size - 44) // alignment
    header_frames = data_size // alignment
    return min(disk_frames, header_frames), rate, channels


def read_wav_mono_f32(dir_fd: int, filename: str, start_frame: int, count: int, channels: int):
    """Read [start_frame, start_frame + count) PCM16 frames read-only as mono float32."""
    import numpy as np

    if count <= 0:
        return np.empty(0, dtype="<f4")
    alignment = channels * 2
    expected_bytes = count * alignment
    with regular(dir_fd, filename) as (stream, _info):
        stream.seek(44 + start_frame * alignment)
        raw = stream.read(expected_bytes)
    if len(raw) != expected_bytes:
        raise ValueError("Committed PCM frames unavailable")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1, dtype=np.float32)
    return (samples / np.float32(32768)).astype("<f4", copy=False)


def _track_kind(track: dict, filename: str) -> str:
    kind = track.get("kind")
    if isinstance(kind, str) and kind in KINDS:
        return kind
    return {"track-microphone.wav": "microphone", "track-system.wav": "system"}.get(filename, "unknown")


def _span_rows(track: dict) -> list[dict]:
    spans = track.get("spans")
    if isinstance(spans, list) and spans:
        return [s for s in spans if isinstance(s, dict)]
    return [track]


def _has_clock_anchors(rows: list[dict]) -> bool:
    return any(isinstance(row.get("clockAnchors"), list) and len(row["clockAnchors"]) > 0 for row in rows)


@dataclass
class _ReadySegment:
    start_sample: int
    end_sample: int
    reason: str
    samples: object  # np.ndarray float32
    session_ms: int


class _SpanBuffer:
    """Bounded sequential 48 kHz mono float64 buffer for one physical WAV span."""

    def __init__(self, filename: str, rate: int, channels: int):
        import numpy as np

        self.filename = filename
        self.rate = rate
        self.channels = channels
        self.read_cursor = 0
        self.base = 0
        self.buffer = np.empty(0, dtype=np.float64)

    def _fill(self, dir_fd: int, end: int, committed: int) -> None:
        import numpy as np

        target = min(end, committed)
        have = self.base + len(self.buffer)
        if target <= have:
            return
        need = target - self.read_cursor
        if need <= 0:
            return
        # Read in <= 1 s chunks to keep temporary buffers bounded.
        parts = [self.buffer] if len(self.buffer) else []
        while self.read_cursor < target:
            step = min(RATE_48K, target - self.read_cursor)
            block = read_wav_mono_f32(dir_fd, self.filename, self.read_cursor, step, self.channels)
            parts.append(block.astype(np.float64))
            self.read_cursor += step
        self.buffer = np.concatenate(parts) if len(parts) > 1 else parts[0]

    def get(self, dir_fd: int, start: int, end: int, committed: int):
        import numpy as np

        if start < self.base:
            raise ValueError("Span reader moved backwards")
        self._fill(dir_fd, end, committed)
        out = np.zeros(max(0, end - start), dtype=np.float64)
        lo = max(start, 0)
        hi = min(end, committed, self.base + len(self.buffer))
        if lo < hi:
            out[lo - start : hi - start] = self.buffer[lo - self.base : hi - self.base]
        return out

    def release(self, before: int) -> None:
        if before > self.base:
            drop = min(before - self.base, len(self.buffer))
            self.buffer = self.buffer[drop:]
            self.base += drop


class TrackFollower:
    """Incremental read-only follower for one logical session.json track."""

    def __init__(self, track_id: str, order: int, track: dict, events: list, detector):
        import numpy as np

        from . import recognition_fallback as fallback
        from .speech_pipeline import IncrementalResampler, SpeechGate

        self.track_id = track_id
        self.order = order
        self.primary_file = str(track.get("file", ""))
        self.kind = _track_kind(track, self.primary_file)
        raw_offset = track.get("startOffsetMs")
        self.offset_ms = float(raw_offset) if isinstance(raw_offset, (int, float)) and not isinstance(raw_offset, bool) else None
        self.placed_offset = 0.0 if self.offset_ms is None else self.offset_ms
        self.origin_16k = placement.frame(self.placed_offset, RATE_16K)
        self.sample_rate: int | None = None
        self.channels: int | None = None
        self.unsupported = False
        self.done = False
        self.flushed = False
        self.prefix_invalidated = False
        self.ever_clock_anchored = False

        # Snapshot of latest session.json metadata for this track.
        self._rows: list[dict] = _span_rows(track)
        self._track_ended = bool(track.get("ended"))
        self._session_ended = False
        self.ever_clock_anchored = _has_clock_anchors(self._rows)
        self._ownership = ownership_windows(self.kind, events) if self.offset_ms is not None else None
        self._mask = FrameMask(self._ownership, RATE_16K, self.origin_16k / 16)

        # Streaming audio state.
        self._span_buffers: dict[str, _SpanBuffer] = {}
        self._tl_cursor_48k = 0  # Next 48 kHz timeline sample to emit (for 48 kHz path)
        self._cursor_16k_raw = 0  # Next raw 16 kHz source sample (for direct 16 kHz path)
        self._resampler = IncrementalResampler()

        # 16 kHz VAD / gate / meter / prefix state.
        self._emitted_16k = 0
        self._pending_16k = np.empty(0, dtype="<f4")
        self._prefix_hash_buf = bytearray()
        self._detector = detector
        detector.reset()
        self._detector_state = detector.snapshot()
        self._gate = SpeechGate(detector)
        self._meter = fallback.ActivityMeter(None)
        self._closed_ranges: list[dict] = []
        self._open_start: int | None = None
        self._open_pieces: list = []
        self._block_hashes: list[bytes] = []
        self.latest_prefix = None

        # Bounded queue of closed segments awaiting ASR (typically length 0 or 1).
        self.ready_segments: deque[_ReadySegment] = deque()
        self._seen_segment_count = 0

    def update(self, track: dict, events: list, session_ended: bool) -> None:
        self._rows = _span_rows(track)
        self._track_ended = bool(track.get("ended"))
        self._session_ended = bool(session_ended)
        if _has_clock_anchors(self._rows):
            if not self.ever_clock_anchored and (
                self._emitted_16k > 0 or self._tl_cursor_48k > 0 or self._cursor_16k_raw > 0
            ):
                # Clock anchors appeared only after unwarped decoding began: fall back to batch.
                self.prefix_invalidated = True
                self.unsupported = True
            self.ever_clock_anchored = True
        offset_changed = False
        if self.offset_ms is None:
            raw_offset = track.get("startOffsetMs")
            if isinstance(raw_offset, (int, float)) and not isinstance(raw_offset, bool):
                # Offset became known after initial empty track row.
                if self._emitted_16k == 0 and self._tl_cursor_48k == 0 and self._cursor_16k_raw == 0:
                    self.offset_ms = float(raw_offset)
                    self.placed_offset = self.offset_ms
                    self.origin_16k = placement.frame(self.placed_offset, RATE_16K)
                    offset_changed = True
                else:
                    self.prefix_invalidated = True
        new_ownership = ownership_windows(self.kind, events) if self.offset_ms is not None else None
        if new_ownership != self._ownership or offset_changed:
            new_mask = FrameMask(new_ownership, RATE_16K, self.origin_16k / 16)
            if self._emitted_16k > 0 and (
                self._mask.sections(0, self._emitted_16k) != new_mask.sections(0, self._emitted_16k)
            ):
                self.prefix_invalidated = True
            self._ownership = new_ownership
            self._mask = new_mask

    def has_ready_segment(self) -> bool:
        return bool(self.ready_segments)

    def next_session_time_ms(self) -> int:
        if self.ready_segments:
            return self.ready_segments[0].session_ms
        return placement.word_ms(self.placed_offset, self._emitted_16k)

    def pop_ready_segment(self) -> _ReadySegment:
        return self.ready_segments.popleft()

    def _inspect_spans(self, dir_fd: int) -> list[dict] | None:
        if (
            not self._session_ended
            and not self._track_ended
            and self._emitted_16k == 0
            and self._tl_cursor_48k == 0
            and self._cursor_16k_raw == 0
        ):
            manifest_frames = sum(
                row["frames"] for row in self._rows if type(row.get("frames")) is int and row["frames"] > 0
            )
            if manifest_frames == 0:
                return None
        inspected = []
        for idx, row in enumerate(self._rows):
            if "codec" in row:
                self.unsupported = True
                return None
            filename = row.get("file")
            if not isinstance(filename, str) or not filename.startswith("track-") or not filename.endswith(".wav"):
                return None
            info = read_wav_committed(dir_fd, filename)
            if info is None:
                return None
            committed, rate, channels = info
            if self.sample_rate is None:
                self.sample_rate, self.channels = rate, channels
            if rate not in (RATE_16K, RATE_48K):
                self.unsupported = True
                return None
            span_offset = row.get("startOffsetMs")
            if span_offset is None:
                span_offset = self.placed_offset
            elif type(span_offset) not in (int, float) or not math.isfinite(span_offset) or span_offset < 0:
                self.unsupported = True
                return None
            span_ended = self._session_ended or self._track_ended or bool(row.get("ended")) or (idx < len(self._rows) - 1)
            anchors = row.get("clockAnchors") if isinstance(row.get("clockAnchors"), list) else []
            inspected.append(
                dict(
                    file=filename,
                    committed=committed,
                    rate=rate,
                    channels=channels,
                    start_offset_ms=float(span_offset),
                    ended=span_ended,
                    anchors=anchors,
                )
            )
        if not inspected:
            return None
        # Direct 16 kHz only applies to single-span unwarped tracks; multi-span or warped 16 kHz
        # uses GStreamer resampling in mapped_audio, so leave it to post-Stop batch.
        if self.sample_rate == RATE_16K and (len(inspected) > 1 or self.ever_clock_anchored or _has_clock_anchors(self._rows)):
            self.unsupported = True
            self.prefix_invalidated = True
            return None
        if any(s["rate"] != self.sample_rate for s in inspected):
            self.unsupported = True
            self.prefix_invalidated = True
            return None
        return inspected

    def _safe_48k_plan(self, spans: list[dict]):
        """Return (span_specs, safe_tl_end_48k, reached_final_eof) for 48 kHz single/multi/warped tracks."""
        from .library_v2 import mixdown

        specs = []
        all_ended = self._session_ended or self._track_ended
        safe_end = 0
        final_end = 0
        blocked_by_unanchored = False

        for idx, span in enumerate(spans):
            local_16k = placement.frame(span["start_offset_ms"], RATE_16K) - self.origin_16k
            if local_16k < 0:
                self.unsupported = True
                return None, 0, False
            shift_48k = 3 * local_16k
            start_ms_local = local_16k / 16
            anchors = span["anchors"]
            span_has_anchors = bool(anchors) or self.ever_clock_anchored
            committed = span["committed"]

            if not span_has_anchors:
                mapping = mixdown.Mapping(
                    SimpleNamespace(
                        start_ms=start_ms_local,
                        anchors=(),
                        info=SimpleNamespace(sample_rate=RATE_48K),
                    )
                )
                specs.append((span, mapping, committed))
                if span["ended"]:
                    span_safe = shift_48k + committed
                    if not blocked_by_unanchored:
                        safe_end = max(safe_end, span_safe)
                elif committed < RIGHT_HALO_48K:
                    # Wait for the new rotated span to have committed frames before crossing its start.
                    if idx > 0:
                        safe_end = min(safe_end, max(0, shift_48k))
                    else:
                        safe_end = 0
                else:
                    span_safe = shift_48k + (committed - RIGHT_HALO_48K)
                    if not blocked_by_unanchored:
                        safe_end = max(safe_end, span_safe)
                final_end = max(final_end, shift_48k + committed)
                continue

            # Clock-anchored native span.
            if span["ended"]:
                usable_anchors = anchors
                mapping = mixdown.Mapping(
                    SimpleNamespace(
                        start_ms=start_ms_local,
                        anchors=tuple(tuple(a) for a in usable_anchors if isinstance(a, list)),
                        info=SimpleNamespace(sample_rate=RATE_48K),
                    )
                )
                specs.append((span, mapping, committed))
                span_end = mapping.end(committed) if committed > 0 else shift_48k
                if not blocked_by_unanchored:
                    safe_end = max(safe_end, span_end)
                final_end = max(final_end, span_end)
            else:
                # Live anchored span: only anchors[:-1] are permanently fixed.
                fixed_raw = anchors[:-1] if len(anchors) >= 2 else []
                knots = mixdown.valid_anchors(fixed_raw, RATE_48K)
                if len(knots) < 2:
                    # Wait until at least one 60 s interval is permanently anchored.
                    if idx > 0:
                        safe_end = min(safe_end, max(0, shift_48k - 1))
                    else:
                        safe_end = 0
                    blocked_by_unanchored = True
                    continue
                mapping = mixdown.Mapping(
                    SimpleNamespace(
                        start_ms=start_ms_local,
                        anchors=tuple(tuple(a) for a in fixed_raw if isinstance(a, list)),
                        info=SimpleNamespace(sample_rate=RATE_48K),
                    )
                )
                if mapping.knots is None:
                    blocked_by_unanchored = True
                    continue
                j_arr, p_arr = mapping.knots
                # Find rightmost permanently fixed knot whose linear-interp neighbour is committed.
                best_p = None
                for k in range(len(j_arr) - 1, 0, -1):
                    if committed >= int(math.floor(float(j_arr[k]))) + 2:
                        best_p = float(p_arr[k])
                        break
                if best_p is None:
                    if idx > 0:
                        safe_end = min(safe_end, max(0, shift_48k - 1))
                    else:
                        safe_end = 0
                    blocked_by_unanchored = True
                    continue
                specs.append((span, mapping, committed))
                # Strictly before the right knot so mapping.source never extrapolates past fixed knots.
                span_safe = max(0, int(math.floor(best_p)) - 1)
                if not blocked_by_unanchored:
                    safe_end = max(safe_end, span_safe)

        if all_ended and not blocked_by_unanchored:
            return specs, final_end, True
        return specs, safe_end, False

    def _emit_48k_block(self, dir_fd: int, specs: list, start: int, count: int):
        import numpy as np

        out = np.zeros(count, dtype=np.float64)
        for span, mapping, committed in specs:
            if committed <= 0:
                continue
            buf = self._span_buffers.get(span["file"])
            if buf is None:
                buf = self._span_buffers[span["file"]] = _SpanBuffer(span["file"], span["rate"], span["channels"])
            if mapping.knots is None:
                first = start - mapping.shift
                lo, hi = max(first, 0), min(first + count, committed)
                if lo < hi:
                    out[lo - first : hi - first] += buf.get(dir_fd, lo, hi, committed)
                    buf.release(hi)
                continue
            j = mapping.source(np.arange(start, start + count, dtype=np.float64))
            inside = (j >= 0) & (j <= committed - 1)
            if not inside.any():
                continue
            j_in = j[inside]
            low = np.floor(j_in).astype(np.int64)
            weight = j_in - low
            first = int(low.min())
            segment = buf.get(dir_fd, first, int(low.max()) + 2, committed)
            index = low - first
            out[inside] += segment[index] * (1.0 - weight) + segment[index + 1] * weight
            buf.release(int(low.max()))
        return out.astype("<f4")

    def _apply_mask_16k(self, block):
        import numpy as np

        if not len(block):
            return block
        sections = self._mask.sections(self._emitted_16k, len(block))
        if sections != [(0, len(block))]:
            selected = np.zeros_like(block)
            for left, right in sections:
                selected[left:right] = block[left:right]
            block = selected
        self._emitted_16k += len(block)
        return block

    def _collect_gate_chunks(self, chunks) -> None:
        import numpy as np

        for chunk in chunks:
            if self._open_start is None:
                self._open_start = chunk.start_sample
            self._open_pieces.append(chunk.samples)
            if chunk.end:
                samples = np.concatenate(self._open_pieces) if len(self._open_pieces) > 1 else self._open_pieces[0]
                end_sample = chunk.start_sample + len(chunk.samples)
                self._closed_ranges.append(
                    dict(startSample=self._open_start, endSample=end_sample, reason=chunk.reason)
                )
                session_ms = placement.word_ms(self.placed_offset, self._open_start)
                self.ready_segments.append(
                    _ReadySegment(
                        start_sample=self._open_start,
                        end_sample=end_sample,
                        reason=chunk.reason,
                        samples=samples,
                        session_ms=session_ms,
                    )
                )
                self._seen_segment_count += 1
                self._open_pieces.clear()
                self._open_start = None

    def _feed_16k_masked(self, block, *, flush: bool = False) -> None:
        import numpy as np

        from .speech_pipeline import PREFIX_BLOCK_SAMPLES, SegmentationPrefix

        frame_size = self._detector.frame_size
        block_bytes = PREFIX_BLOCK_SAMPLES * 4
        if len(block):
            block = np.asarray(block, dtype="<f4")
            self._pending_16k = np.concatenate((self._pending_16k, block)) if len(self._pending_16k) else block

        self._detector.restore(self._detector_state)
        try:
            # Feed at 512-sample frame granularity (32 ms) so closed segments emit immediately
            # without waiting for an 8192-sample prefix block boundary, while capturing exact
            # SegmentationPrefix checkpoints on every PREFIX_BLOCK_SAMPLES boundary.
            while len(self._pending_16k) >= frame_size:
                if not self.prefix_invalidated:
                    rem_samples = (block_bytes - len(self._prefix_hash_buf)) // 4
                    take = min((len(self._pending_16k) // frame_size) * frame_size, rem_samples)
                else:
                    take = (len(self._pending_16k) // frame_size) * frame_size
                piece = self._pending_16k[:take]
                self._pending_16k = self._pending_16k[take:]
                self._meter._feed(piece)
                self._collect_gate_chunks(self._gate.feed(piece))
                if not self.prefix_invalidated:
                    self._prefix_hash_buf.extend(piece.tobytes())
                    if len(self._prefix_hash_buf) == block_bytes:
                        self._block_hashes.append(hashlib.sha256(self._prefix_hash_buf).digest())
                        self._prefix_hash_buf.clear()
                        self.latest_prefix = SegmentationPrefix(
                            samples=len(self._block_hashes) * PREFIX_BLOCK_SAMPLES,
                            block_hashes=tuple(self._block_hashes),
                            ownership_windows=self._ownership,
                            offset_ms=self.placed_offset,
                            vad_input_stage=self._detector.input_stage,
                            max_samples=self._gate.max_samples,
                            detector_state=self._detector.snapshot(),
                            gate_state=self._gate.snapshot(),
                            meter_codes=bytes(self._meter.codes),
                            closed_ranges=tuple(dict(r) for r in self._closed_ranges),
                            open_start=self._open_start,
                        )
            if flush:
                if len(self._pending_16k):
                    tail = self._pending_16k
                    self._pending_16k = np.empty(0, dtype="<f4")
                    self._meter._feed(tail)
                    self._collect_gate_chunks(self._gate.feed(tail))
                self._collect_gate_chunks(self._gate.flush())
                self.flushed = True
                self.done = True
        finally:
            self._detector_state = self._detector.snapshot()

    def can_advance(self, dir_fd: int) -> bool:
        if self.unsupported or self.done or self.has_ready_segment():
            return False
        spans = self._inspect_spans(dir_fd)
        if spans is None:
            return False
        if self.sample_rate == RATE_16K:
            span = spans[0]
            if span["committed"] > self._cursor_16k_raw:
                return True
            return bool((self._session_ended or self._track_ended) and not self.flushed)
        specs, safe_end, reached_eof = self._safe_48k_plan(spans)
        if specs is None:
            return False
        if safe_end > self._tl_cursor_48k:
            return True
        return bool(reached_eof and not self.flushed)

    def advance_quantum(self, dir_fd: int) -> bool:
        """Advance this track by at most one bounded quantum (~512 ms)."""
        import numpy as np

        if self.unsupported or self.done or self.has_ready_segment():
            return False
        spans = self._inspect_spans(dir_fd)
        if spans is None:
            return False

        if self.sample_rate == RATE_16K:
            span = spans[0]
            avail = span["committed"] - self._cursor_16k_raw
            if avail > 0:
                take = min(QUANTUM_16K, avail)
                block = read_wav_mono_f32(dir_fd, span["file"], self._cursor_16k_raw, take, span["channels"])
                self._cursor_16k_raw += take
                masked = self._apply_mask_16k(block)
                should_flush = (self._session_ended or self._track_ended) and self._cursor_16k_raw >= span["committed"]
                self._feed_16k_masked(masked, flush=should_flush)
                return True
            if (self._session_ended or self._track_ended) and not self.flushed:
                self._feed_16k_masked(np.empty(0, dtype="<f4"), flush=True)
                return True
            return False

        specs, safe_end, reached_eof = self._safe_48k_plan(spans)
        if specs is None:
            return False
        avail = safe_end - self._tl_cursor_48k
        if avail > 0:
            take = min(QUANTUM_48K, avail)
            block_48k = self._emit_48k_block(dir_fd, specs, self._tl_cursor_48k, take)
            self._tl_cursor_48k += take
            for block_16k in self._resampler.feed(block_48k):
                self._feed_16k_masked(self._apply_mask_16k(block_16k), flush=False)
            if reached_eof and self._tl_cursor_48k >= safe_end:
                for block_16k in self._resampler.flush():
                    self._feed_16k_masked(self._apply_mask_16k(block_16k), flush=False)
                self._feed_16k_masked(np.empty(0, dtype="<f4"), flush=True)
            return True
        if reached_eof and not self.flushed:
            for block_16k in self._resampler.flush():
                self._feed_16k_masked(self._apply_mask_16k(block_16k), flush=False)
            self._feed_16k_masked(np.empty(0, dtype="<f4"), flush=True)
            return True
        return False

    def export_prefix(self):
        if self.prefix_invalidated or self.unsupported or self.latest_prefix is None:
            return None
        return self.latest_prefix


class SessionFollower:
    """Multi-track earliest-session-time precompute coordinator (read-only on capture)."""

    def __init__(self, directory, model, model_identity, detector, *, on_cache_entry):
        self.directory = Path(directory)
        try:
            self.resolved_dir = str(self.directory.resolve())
        except OSError:
            self.resolved_dir = str(self.directory.absolute())
        self.model = model
        self.model_identity = model_identity
        self.detector = detector
        self.on_cache_entry = on_cache_entry
        self.tracks: dict[str, TrackFollower] = {}
        self.session_ended = False
        self.segments_decoded = 0
        self.schedule_trace: list[tuple[str, str, int]] = []  # (track_id, action, session_ms) for diagnostics/tests

    def refresh(self) -> bool:
        manifest = read_session_readonly(self.directory)
        if manifest is None:
            return False
        state = manifest.get("state")
        if state in ("completed", "interrupted"):
            self.session_ended = True
        elif state != "recording":
            return False
        events = manifest.get("events") if isinstance(manifest.get("events"), list) else []
        tracks = manifest.get("tracks") if isinstance(manifest.get("tracks"), list) else []
        for order, track in enumerate(tracks[:64]):
            if not isinstance(track, dict):
                continue
            track_id = track.get("id")
            if not isinstance(track_id, str) or not track_id:
                continue
            follower = self.tracks.get(track_id)
            if follower is None:
                follower = TrackFollower(track_id, order, track, events, self.detector)
                self.tracks[track_id] = follower
            else:
                follower.order = order
                follower.update(track, events, self.session_ended)
        return True

    def step(self) -> bool:
        """Perform one bounded unit of work for the earliest-session-time ready track."""
        from .speech_pipeline import raw_result_to_cache_entry

        if not self.tracks:
            return False
        try:
            dir_fd = _open_session_dir_ro(self.directory)
        except OSError:
            return False
        try:
            candidates = []
            for track in self.tracks.values():
                if track.has_ready_segment():
                    candidates.append((track.next_session_time_ms(), track.order, "recognize", track))
                elif track.can_advance(dir_fd):
                    candidates.append((track.next_session_time_ms(), track.order, "advance", track))
            if not candidates:
                return False
            candidates.sort(key=lambda item: (item[0], item[1]))
            session_ms, _order, action, chosen = candidates[0]
            if action == "advance":
                did = chosen.advance_quantum(dir_fd)
                if did:
                    self.schedule_trace.append((chosen.track_id, "advance", session_ms))
                return did
        finally:
            os.close(dir_fd)

        # Recognize outside any open capture file descriptor.
        ready = chosen.pop_ready_segment()
        self.schedule_trace.append((chosen.track_id, "recognize", ready.session_ms))
        result = self.model.transcribe(ready.samples, language=None, word_timestamps=True)
        entry, _digest = raw_result_to_cache_entry(self.model_identity, ready.samples, result)
        if entry is not None:
            self.segments_decoded += 1
            self.on_cache_entry(entry)
        return True

    def all_drained(self) -> bool:
        if not self.session_ended:
            return False
        return all(
            (track.done or track.unsupported) and not track.has_ready_segment() for track in self.tracks.values()
        )

    def export_prefixes(self) -> dict:
        prefixes = {}
        for track in self.tracks.values():
            prefix = track.export_prefix()
            if prefix is None:
                continue
            prefixes[(self.resolved_dir, track.track_id)] = prefix
            if track.primary_file:
                prefixes[(self.resolved_dir, track.primary_file)] = prefix
        return prefixes


def run_follow_loop(model, bundle: Path, message: dict, emit, *, reader: StdinLineReader, detector, prefix_store: dict):
    """Run read-only session following inside a verified speech worker until follow-stop/quit.

    Returns (next_command_or_None, detector). If a non-follow command was received
    or EOF/quit occurred, handles clean exit while keeping detector and prefix_store
    warm for an immediate post-Stop pipeline handoff.
    """
    from .speech_pipeline import SpeechDetector

    directory = message.get("directory")
    model_identity = message.get("model")
    if not isinstance(directory, str) or not directory or not is_valid_model_identity(model_identity):
        raise ValueError("Invalid follow request")
    poll_ms = message.get("pollIntervalMs", 50)
    if type(poll_ms) not in (int, float) or not math.isfinite(poll_ms) or not 5 <= poll_ms <= 2000:
        poll_ms = 50
    poll_s = float(poll_ms) / 1000.0

    if detector is None:
        detector = SpeechDetector(bundle / "silero_vad.onnx")

    def on_cache(entry):
        emit("follow-cache", directory=directory, cacheEntry=entry)

    follower = SessionFollower(directory, model, model_identity, detector, on_cache_entry=on_cache)
    idle_notified = False
    while True:
        # Check for stop/quit/preempt commands between every bounded step.
        line = reader.try_readline(0.0)
        if line is not None:
            if not line:
                return None, detector
            cmd = json.loads(line)
            cmd_type = cmd.get("type")
            if cmd_type == "follow-stop":
                exported = follower.export_prefixes()
                prefix_store.clear()
                prefix_store.update(exported)
                emit(
                    "follow-stopped",
                    directory=directory,
                    segments=follower.segments_decoded,
                    prefixes=len(exported),
                )
                return "continue", detector
            if cmd_type == "quit":
                return "quit", detector
            raise ValueError(f"Unexpected command during follow mode: {cmd_type}")

        try:
            follower.refresh()
            worked = follower.step()
        except (OSError, ValueError):
            worked = False

        if worked:
            idle_notified = False
            continue

        if follower.all_drained() and not idle_notified:
            exported = follower.export_prefixes()
            prefix_store.clear()
            prefix_store.update(exported)
            emit(
                "follow-idle",
                directory=directory,
                drained=True,
                segments=follower.segments_decoded,
                prefixes=len(exported),
            )
            idle_notified = True

        # Sleep reactively on stdin until either a command arrives or poll_s elapses.
        line = reader.try_readline(poll_s)
        if line is not None:
            if not line:
                return None, detector
            cmd = json.loads(line)
            cmd_type = cmd.get("type")
            if cmd_type == "follow-stop":
                exported = follower.export_prefixes()
                prefix_store.clear()
                prefix_store.update(exported)
                emit(
                    "follow-stopped",
                    directory=directory,
                    segments=follower.segments_decoded,
                    prefixes=len(exported),
                )
                return "continue", detector
            if cmd_type == "quit":
                return "quit", detector
            raise ValueError(f"Unexpected command during follow mode: {cmd_type}")
