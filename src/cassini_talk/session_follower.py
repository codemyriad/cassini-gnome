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
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from pathlib import Path

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


def _stat_session_manifest(directory: Path) -> tuple[int, int, int, int] | None:
    """Return a cheap revision signature for session.json without reading or following symlinks."""
    try:
        dir_fd = _open_session_dir_ro(directory)
    except OSError:
        return None
    try:
        info = os.stat("session.json", dir_fd=dir_fd, follow_symlinks=False)
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    except OSError:
        return None
    finally:
        os.close(dir_fd)


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


def read_wav_mono_f32(dir_fd: int, filename: str, start_frame: int, count: int, channels: int, *, raw_hasher=None):
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
    if raw_hasher is not None:
        raw_hasher.update(raw)
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
        self.raw_hasher = hashlib.sha256()
        self._cached_hash_cursor = 0
        self._cached_raw_sha256 = self.raw_hasher.hexdigest()

    def raw_sha256(self) -> str:
        if self._cached_hash_cursor != self.read_cursor:
            self._cached_raw_sha256 = self.raw_hasher.hexdigest()
            self._cached_hash_cursor = self.read_cursor
        return self._cached_raw_sha256

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
            block = read_wav_mono_f32(
                dir_fd, self.filename, self.read_cursor, step, self.channels, raw_hasher=self.raw_hasher
            )
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


class _IncrementalSpanMapping:
    """O(1)-per-anchor incremental clock mapping matching `mixdown.Mapping` bit-for-bit."""

    def __init__(self, start_ms_local: float, rate: int = RATE_48K):
        from .library_v2 import mixdown

        self.start_ms_local = start_ms_local
        self.rate = rate
        self.origin = mixdown.frame(start_ms_local, RATE_48K)
        self.shift = self.origin
        self._raw_count = 0
        self._last_raw: tuple[int, int] | None = None
        self._kept_raw: list[tuple[int, int]] = []
        self.j_list: list[float] = []
        self.p_list: list[float] = []
        self._at_zero: float | None = None
        self.knots_snapshot: tuple[tuple[float, ...], tuple[float, ...]] | None = None

    @property
    def knots(self):
        return (self.j_list, self.p_list) if len(self.j_list) >= 2 else None

    def _refresh_knots_snapshot(self) -> None:
        self.knots_snapshot = (tuple(self.j_list), tuple(self.p_list)) if len(self.j_list) >= 2 else None

    def _append_valid(self, index: int, ns: int) -> None:
        from .library_v2 import mixdown

        if self._kept_raw:
            last_index, last_ns = self._kept_raw[-1]
            if index <= last_index or ns <= last_ns:
                return
            slope = ((ns - last_ns) / 1e9) / ((index - last_index) / self.rate)
            if abs(slope - 1) > mixdown.MAX_SLOPE_ERROR:
                return
        self._kept_raw.append((index, ns))
        if len(self._kept_raw) == 2:
            j0 = self._kept_raw[0][0] * RATE_48K / self.rate
            j1 = self._kept_raw[1][0] * RATE_48K / self.rate
            s0 = self._kept_raw[0][1] / 1e9
            s1 = self._kept_raw[1][1] / 1e9
            self._at_zero = s0 + (0.0 - j0) * ((s1 - s0) / (j1 - j0))
            self.j_list = [j0, j1]
            self.p_list = [
                self.origin + (s0 - self._at_zero) * RATE_48K,
                self.origin + (s1 - self._at_zero) * RATE_48K,
            ]
            self._refresh_knots_snapshot()
        elif len(self._kept_raw) > 2:
            self.j_list.append(index * RATE_48K / self.rate)
            self.p_list.append(self.origin + (ns / 1e9 - self._at_zero) * RATE_48K)
            self._refresh_knots_snapshot()

    def _rebuild(self, anchors) -> None:
        from .library_v2 import mixdown

        self._raw_count = len(anchors)
        self._last_raw = (
            (anchors[-1][0], anchors[-1][1])
            if anchors and isinstance(anchors[-1], (list, tuple)) and len(anchors[-1]) == 2
            else None
        )
        self._kept_raw.clear()
        self.j_list.clear()
        self.p_list.clear()
        self._at_zero = None
        self.knots_snapshot = None
        knots = mixdown.valid_anchors(anchors, self.rate)
        if len(knots) < 2:
            return
        self._kept_raw = [(idx, round(t * 1e9)) for idx, t in knots]
        j0 = knots[0][0] * RATE_48K / self.rate
        j1 = knots[1][0] * RATE_48K / self.rate
        s0, s1 = knots[0][1], knots[1][1]
        self._at_zero = s0 + (0.0 - j0) * ((s1 - s0) / (j1 - j0))
        self.j_list = [idx * RATE_48K / self.rate for idx, _ in knots]
        self.p_list = [self.origin + (t - self._at_zero) * RATE_48K for _, t in knots]
        self._refresh_knots_snapshot()

    def sync(self, anchors, count: int) -> None:
        """Synchronize mapping against `anchors[:count]` in O(1) when monotonically appended."""
        if count == 0:
            if self._raw_count != 0:
                self._raw_count = 0
                self._last_raw = None
                self._kept_raw.clear()
                self.j_list.clear()
                self.p_list.clear()
                self._at_zero = None
                self.knots_snapshot = None
            return
        if count == self._raw_count:
            tail = anchors[count - 1]
            if isinstance(tail, (list, tuple)) and len(tail) == 2 and (tail[0], tail[1]) == self._last_raw:
                return
        if count > self._raw_count:
            prefix_ok = self._raw_count == 0
            if not prefix_ok:
                prev = anchors[self._raw_count - 1]
                prefix_ok = isinstance(prev, (list, tuple)) and len(prev) == 2 and (prev[0], prev[1]) == self._last_raw
            if prefix_ok:
                prev_key = self._last_raw
                monotonic = True
                parsed_new = []
                for item in anchors[self._raw_count : count]:
                    if (
                        isinstance(item, (list, tuple))
                        and len(item) == 2
                        and type(item[0]) is int
                        and type(item[1]) is int
                        and 0 <= item[0] <= 2**63
                        and 0 <= item[1] <= 2**63
                    ):
                        pair = (item[0], item[1])
                        if prev_key is not None and pair < prev_key:
                            monotonic = False
                            break
                        prev_key = pair
                        parsed_new.append(pair)
                if monotonic:
                    for index, ns in parsed_new:
                        self._append_valid(index, ns)
                    self._raw_count = count
                    self._last_raw = prev_key
                    return
        self._rebuild(anchors[:count])

    def source(self, positions):
        import numpy as np

        from .library_v2 import mixdown

        positions = np.asarray(positions, dtype=np.float64)
        if len(self.j_list) < 2:
            return positions - self.shift
        if not len(positions):
            return np.empty(0, dtype=np.float64)
        lo = max(0, bisect_right(self.p_list, float(positions[0])) - 1)
        hi = min(len(self.p_list), bisect_right(self.p_list, float(positions[-1])) + 1)
        if hi - lo < 2:
            if lo > 0:
                lo -= 1
            else:
                hi = min(len(self.p_list), lo + 2)
        p_slice = np.asarray(self.p_list[lo:hi], dtype=np.float64)
        j_slice = np.asarray(self.j_list[lo:hi], dtype=np.float64)
        return mixdown._linear(positions, p_slice, j_slice)

    def end(self, length: int) -> int:
        import numpy as np

        from .library_v2 import mixdown

        if len(self.j_list) < 2:
            return round(float(length) + self.shift)
        idx = bisect_right(self.j_list, float(length))
        lo = max(0, min(idx - 1, len(self.j_list) - 2))
        hi = min(len(self.j_list), lo + 2)
        j_slice = np.asarray(self.j_list[lo:hi], dtype=np.float64)
        p_slice = np.asarray(self.p_list[lo:hi], dtype=np.float64)
        return round(float(mixdown._linear(np.array([float(length)]), j_slice, p_slice)[0]))


@dataclass(frozen=True)
class _PrefixCheckpoint:
    """O(1) checkpoint into TrackFollower's append-only prefix histories."""

    block_count: int
    meter_code_count: int
    closed_range_count: int
    ownership_windows: object
    offset_ms: int
    vad_input_stage: str
    max_samples: int
    detector_state: object
    gate_state: object
    open_start: int | None


class TrackFollower:
    """Incremental read-only follower for one logical session.json track."""

    def __init__(self, track_id: str, order: int, track: dict, events: list, detector, *, session_ended: bool = False):
        import numpy as np

        from . import recognition_fallback as fallback
        from .speech_pipeline import IncrementalResampler, SpeechGate

        self.track_id = track_id
        self.order = order
        self.primary_file = str(track.get("file", ""))
        self.kind = _track_kind(track, self.primary_file)
        raw_offset = track.get("startOffsetMs")
        self.offset_ms = (
            float(raw_offset) if isinstance(raw_offset, (int, float)) and not isinstance(raw_offset, bool) else None
        )
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
        self._session_ended = bool(session_ended)
        self.ever_clock_anchored = _has_clock_anchors(self._rows)
        self._ownership = ownership_windows(self.kind, events) if self.offset_ms is not None else None
        self._mask = FrameMask(self._ownership, RATE_16K, self.origin_16k / 16)

        # Streaming audio state.
        self._span_buffers: dict[str, _SpanBuffer] = {}
        self._span_mappings: dict[str, _IncrementalSpanMapping] = {}
        self._sealed_wav_info: dict[str, tuple[int, int, int]] = {}
        self._inspected_spans: list[dict] = []
        self._sealed_span_count = 0
        self._plan_specs: list[tuple] = []
        self._sealed_plan_count = 0
        self._sealed_safe_end = 0
        self._sealed_final_end = 0
        self._sealed_max_committed = 0
        self._sealed_specs_monotonic = True
        self._sealed_last_start_48k: float | None = None
        self._specs_monotonic = True
        self._last_max_committed_48k = 0
        self._emit_start_idx = 0
        self._tl_cursor_48k = 0  # Next 48 kHz timeline sample to emit (for 48 kHz path)
        self._cursor_16k_raw = 0  # Next raw 16 kHz source sample (for direct 16 kHz path)
        self._raw_hasher_16k = hashlib.sha256()
        self._resampler = IncrementalResampler()
        self._last_spans: list[dict] = []
        self._last_specs: list[tuple] = []
        self._last_total_end_ms = 0
        self._ended_span_provs: list[object] = []
        self._diarization_stager = None

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
        self._latest_checkpoint: _PrefixCheckpoint | None = None

        # Bounded queue of closed segments awaiting ASR (typically length 0 or 1).
        self.ready_segments: deque[_ReadySegment] = deque()
        self._seen_segment_count = 0

    def _reset_sealed_span_caches(self) -> None:
        self._inspected_spans.clear()
        self._sealed_span_count = 0
        self._plan_specs.clear()
        self._sealed_plan_count = 0
        self._sealed_safe_end = 0
        self._sealed_final_end = 0
        self._sealed_max_committed = 0
        self._sealed_specs_monotonic = True
        self._sealed_last_start_48k = None
        self._specs_monotonic = True
        self._last_max_committed_48k = 0
        self._emit_start_idx = 0

    def update(self, track: dict, events: list, session_ended: bool) -> None:
        self._rows = _span_rows(track)
        self._track_ended = bool(track.get("ended"))
        self._session_ended = bool(session_ended)
        if _has_clock_anchors(self._rows):
            if not self.ever_clock_anchored:
                self._reset_sealed_span_caches()
                if self._emitted_16k > 0 or self._tl_cursor_48k > 0 or self._cursor_16k_raw > 0:
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
        if offset_changed or len(self._rows) < self._sealed_span_count:
            self._reset_sealed_span_caches()
        new_ownership = ownership_windows(self.kind, events) if self.offset_ms is not None else None
        if new_ownership != self._ownership or offset_changed:
            new_mask = FrameMask(new_ownership, RATE_16K, self.origin_16k / 16)
            if self._emitted_16k > 0 and (
                self._mask.sections(0, self._emitted_16k) != new_mask.sections(0, self._emitted_16k)
            ):
                self.prefix_invalidated = True
            self._ownership = new_ownership
            self._mask = new_mask
        if self.prefix_invalidated or self.unsupported:
            self.detach_diarization_stager()

    def attach_diarization_stager(self, stager) -> bool:
        if self.prefix_invalidated or self.unsupported or self._emitted_16k > 0:
            stager.close()
            return False
        if self._diarization_stager is not None and self._diarization_stager is not stager:
            self._diarization_stager.close()
        self._diarization_stager = stager
        return True

    def detach_diarization_stager(self) -> None:
        stager, self._diarization_stager = self._diarization_stager, None
        if stager is not None:
            stager.close()

    def has_staged_diarization(self) -> bool:
        stager = self._diarization_stager
        return (
            stager is not None
            and not stager.aborted
            and not self.prefix_invalidated
            and not self.unsupported
            and stager._samples > 0
        )

    def export_diarization_staged(self):
        stager, self._diarization_stager = self._diarization_stager, None
        if stager is None:
            return None
        if self.prefix_invalidated or self.unsupported:
            stager.close()
            return None
        return stager.export()

    def _build_staged_spans_provenance(self, s0: int):
        import numpy as np

        from .diarization_audio import StagedSpanProvenance

        if self.sample_rate == RATE_16K:
            if not self._last_spans:
                return None
            span = self._last_spans[0]
            committed = span["committed"]
            consumed = self._cursor_16k_raw
            return (
                StagedSpanProvenance(
                    file=span["file"],
                    rate=RATE_16K,
                    channels=span["channels"],
                    start_offset_ms=span["start_offset_ms"],
                    ended=bool(span["ended"] and consumed == committed),
                    knots=None,
                    consumed_frames=consumed,
                    raw_sha256=self._raw_hasher_16k.hexdigest(),
                ),
            )

        if self.sample_rate != RATE_48K or not self._last_specs:
            return None
        max_prefix_48k = (s0 - 1) * 3 + 48
        prefix_done = len(self._ended_span_provs)
        if prefix_done > len(self._last_specs):
            self._ended_span_provs.clear()
            prefix_done = 0
        last_touching = max(0, prefix_done - 1)
        for idx in range(prefix_done, len(self._last_specs)):
            _span, mapping, _committed = self._last_specs[idx]
            if mapping.knots is None:
                touches = mapping.shift <= max_prefix_48k
            else:
                touches = float(mapping.source(np.array([float(max_prefix_48k)]))[0]) >= 0.0
            if touches:
                last_touching = idx
            else:
                break

        empty_sha = None
        active_tail = []
        advancing_prefix = True
        for idx in range(prefix_done, last_touching + 1):
            span, mapping, committed = self._last_specs[idx]
            fname = span["file"]
            buf = self._span_buffers.get(fname)
            if buf is not None:
                consumed = buf.read_cursor
                raw_sha = buf.raw_sha256()
            else:
                consumed = 0
                if empty_sha is None:
                    empty_sha = hashlib.sha256(b"").hexdigest()
                raw_sha = empty_sha
            fully_ended = bool(span["ended"] and consumed == committed)
            prov = StagedSpanProvenance(
                file=fname,
                rate=span["rate"],
                channels=span["channels"],
                start_offset_ms=span["start_offset_ms"],
                ended=fully_ended,
                knots=mapping.knots_snapshot,
                consumed_frames=consumed,
                raw_sha256=raw_sha,
            )
            if advancing_prefix and fully_ended:
                self._ended_span_provs.append(prov)
            else:
                advancing_prefix = False
                active_tail.append(prov)
        return (self._ended_span_provs, len(self._ended_span_provs), tuple(active_tail))

    def has_ready_segment(self) -> bool:
        return bool(self.ready_segments)

    def next_session_time_ms(self) -> int:
        if self.ready_segments:
            return self.ready_segments[0].session_ms
        return placement.word_ms(self.placed_offset, self._emitted_16k)

    def pop_ready_segment(self) -> _ReadySegment:
        return self.ready_segments.popleft()

    def _inspect_span_row(self, dir_fd: int, idx: int, row: dict) -> dict | None:
        if "codec" in row:
            self.unsupported = True
            return None
        filename = row.get("file")
        if not isinstance(filename, str) or not filename.startswith("track-") or not filename.endswith(".wav"):
            return None
        span_ended = self._session_ended or self._track_ended or bool(row.get("ended")) or (idx < len(self._rows) - 1)
        info = self._sealed_wav_info.get(filename) if span_ended else None
        if info is None:
            info = read_wav_committed(dir_fd, filename)
            if info is None:
                return None
            if span_ended:
                self._sealed_wav_info[filename] = info
        committed, rate, channels = info
        if self.sample_rate is None:
            self.sample_rate, self.channels = rate, channels
        if rate not in (RATE_16K, RATE_48K):
            self.unsupported = True
            return None
        if rate != self.sample_rate:
            self.unsupported = True
            self.prefix_invalidated = True
            return None
        span_offset = row.get("startOffsetMs")
        if span_offset is None:
            span_offset = self.placed_offset
        elif type(span_offset) not in (int, float) or not math.isfinite(span_offset) or span_offset < 0:
            self.unsupported = True
            return None
        anchors = row.get("clockAnchors") if isinstance(row.get("clockAnchors"), list) else []
        return dict(
            file=filename,
            committed=committed,
            rate=rate,
            channels=channels,
            start_offset_ms=float(span_offset),
            ended=span_ended,
            anchors=anchors,
        )

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
        if self._sealed_span_count > len(self._rows):
            self._reset_sealed_span_caches()
        del self._inspected_spans[self._sealed_span_count :]
        for idx in range(self._sealed_span_count, len(self._rows)):
            span = self._inspect_span_row(dir_fd, idx, self._rows[idx])
            if span is None:
                return None
            self._inspected_spans.append(span)
            if span["ended"] and idx == self._sealed_span_count:
                self._sealed_span_count += 1
        if not self._inspected_spans:
            return None
        # Direct 16 kHz only applies to single-span unwarped tracks; multi-span or warped 16 kHz
        # uses GStreamer resampling in mapped_audio, so leave it to post-Stop batch.
        if self.sample_rate == RATE_16K and (len(self._inspected_spans) > 1 or self.ever_clock_anchored):
            self.unsupported = True
            self.prefix_invalidated = True
            return None
        return self._inspected_spans

    def _get_span_mapping(self, filename: str, start_ms_local: float) -> _IncrementalSpanMapping:
        mapping = self._span_mappings.get(filename)
        if mapping is None or mapping.start_ms_local != start_ms_local:
            mapping = _IncrementalSpanMapping(start_ms_local, RATE_48K)
            self._span_mappings[filename] = mapping
        return mapping

    def _safe_48k_plan(self, spans: list[dict]):
        """Return (span_specs, safe_tl_end_48k, reached_final_eof) incrementally for 48 kHz tracks."""
        all_ended = self._session_ended or self._track_ended
        if self._sealed_plan_count > len(spans):
            self._plan_specs.clear()
            self._sealed_plan_count = 0
            self._sealed_safe_end = 0
            self._sealed_final_end = 0
            self._sealed_max_committed = 0
            self._sealed_specs_monotonic = True
            self._sealed_last_start_48k = None
        del self._plan_specs[self._sealed_plan_count :]
        safe_end = self._sealed_safe_end
        final_end = self._sealed_final_end
        max_committed = self._sealed_max_committed
        specs_monotonic = self._sealed_specs_monotonic
        last_start_48k = self._sealed_last_start_48k
        blocked_by_unanchored = False

        for idx in range(self._sealed_plan_count, len(spans)):
            span = spans[idx]
            local_16k = placement.frame(span["start_offset_ms"], RATE_16K) - self.origin_16k
            if local_16k < 0:
                self.unsupported = True
                return None, 0, False
            shift_48k = 3 * local_16k
            start_ms_local = local_16k / 16
            anchors = span["anchors"]
            span_has_anchors = bool(anchors) or self.ever_clock_anchored
            committed = span["committed"]
            mapping = self._get_span_mapping(span["file"], start_ms_local)

            if not span_has_anchors:
                mapping.sync((), 0)
                self._plan_specs.append((span, mapping, committed))
                span_start = float(shift_48k)
                if last_start_48k is not None and span_start < last_start_48k:
                    specs_monotonic = False
                last_start_48k = span_start
                span_end = shift_48k + committed
                max_committed = max(max_committed, span_end)
                if span["ended"]:
                    if not blocked_by_unanchored:
                        safe_end = max(safe_end, span_end)
                elif committed < RIGHT_HALO_48K:
                    # Wait for the new rotated span to have committed frames before crossing its start.
                    safe_end = min(safe_end, max(0, shift_48k)) if idx > 0 else 0
                else:
                    span_safe = shift_48k + (committed - RIGHT_HALO_48K)
                    if not blocked_by_unanchored:
                        safe_end = max(safe_end, span_safe)
                final_end = max(final_end, span_end)
                if span["ended"] and not blocked_by_unanchored and idx == self._sealed_plan_count:
                    self._sealed_plan_count += 1
                    self._sealed_safe_end = safe_end
                    self._sealed_final_end = final_end
                    self._sealed_max_committed = max_committed
                    self._sealed_specs_monotonic = specs_monotonic
                    self._sealed_last_start_48k = last_start_48k
                continue

            # Clock-anchored native span.
            if span["ended"]:
                mapping.sync(anchors, len(anchors))
                self._plan_specs.append((span, mapping, committed))
                span_start = float(mapping.shift) if mapping.knots is None else float(mapping.end(0))
                if last_start_48k is not None and span_start < last_start_48k:
                    specs_monotonic = False
                last_start_48k = span_start
                span_end = mapping.end(committed) if committed > 0 else shift_48k
                max_committed = max(
                    max_committed,
                    mapping.end(committed) if mapping.knots is not None else mapping.shift + committed,
                )
                if not blocked_by_unanchored:
                    safe_end = max(safe_end, span_end)
                final_end = max(final_end, span_end)
                if not blocked_by_unanchored and idx == self._sealed_plan_count:
                    self._sealed_plan_count += 1
                    self._sealed_safe_end = safe_end
                    self._sealed_final_end = final_end
                    self._sealed_max_committed = max_committed
                    self._sealed_specs_monotonic = specs_monotonic
                    self._sealed_last_start_48k = last_start_48k
            else:
                # Live anchored span: only anchors[:-1] are permanently fixed.
                fixed_count = len(anchors) - 1 if len(anchors) >= 2 else 0
                mapping.sync(anchors, fixed_count)
                if mapping.knots is None:
                    # Wait until at least one 60 s interval is permanently anchored.
                    safe_end = min(safe_end, max(0, shift_48k - 1)) if idx > 0 else 0
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
                    safe_end = min(safe_end, max(0, shift_48k - 1)) if idx > 0 else 0
                    blocked_by_unanchored = True
                    continue
                self._plan_specs.append((span, mapping, committed))
                span_start = float(mapping.end(0))
                if last_start_48k is not None and span_start < last_start_48k:
                    specs_monotonic = False
                last_start_48k = span_start
                max_committed = max(max_committed, mapping.end(committed))
                # Strictly before the right knot so mapping.source never extrapolates past fixed knots.
                span_safe = max(0, int(math.floor(best_p)) - 1)
                if not blocked_by_unanchored:
                    safe_end = max(safe_end, span_safe)

        self._specs_monotonic = specs_monotonic
        self._last_max_committed_48k = max_committed if self._plan_specs else safe_end
        if all_ended and not blocked_by_unanchored:
            return self._plan_specs, final_end, True
        return self._plan_specs, safe_end, False

    def _emit_48k_span(self, dir_fd: int, spec: tuple, start: int, count: int, out) -> bool:
        import numpy as np

        span, mapping, committed = spec
        block_end = start + count
        if self._specs_monotonic:
            if mapping.knots is None:
                if mapping.shift >= block_end:
                    return False
            elif float(mapping.source(np.array([float(block_end - 1)]))[0]) < 0.0:
                return False
        if committed <= 0:
            return True
        buf = self._span_buffers.get(span["file"])
        if buf is None:
            buf = self._span_buffers[span["file"]] = _SpanBuffer(span["file"], span["rate"], span["channels"])
        if mapping.knots is None:
            first = start - mapping.shift
            lo, hi = max(first, 0), min(first + count, committed)
            if lo < hi:
                out[lo - first : hi - first] += buf.get(dir_fd, lo, hi, committed)
                buf.release(hi)
            return True
        j = mapping.source(np.arange(start, block_end, dtype=np.float64))
        inside = (j >= 0) & (j <= committed - 1)
        if not inside.any():
            return True
        j_in = j[inside]
        low = np.floor(j_in).astype(np.int64)
        weight = j_in - low
        first = int(low.min())
        segment = buf.get(dir_fd, first, int(low.max()) + 2, committed)
        index = low - first
        out[inside] += segment[index] * (1.0 - weight) + segment[index + 1] * weight
        buf.release(int(low.max()))
        return True

    def _emit_48k_block(self, dir_fd: int, specs: list, start: int, count: int):
        import numpy as np

        if count <= 0:
            return np.empty(0, dtype="<f4")
        if self._emit_start_idx > len(specs):
            self._emit_start_idx = 0
        while self._emit_start_idx < len(specs):
            span, mapping, committed = specs[self._emit_start_idx]
            if not span["ended"]:
                break
            if committed > 0:
                if mapping.knots is None:
                    if mapping.shift + committed > start:
                        break
                elif float(mapping.source(np.array([float(start)]))[0]) <= committed - 1:
                    break
            self._emit_start_idx += 1

        out = np.zeros(count, dtype=np.float64)
        for idx in range(self._emit_start_idx, len(specs)):
            if not self._emit_48k_span(dir_fd, specs[idx], start, count, out):
                break
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

        from .speech_pipeline import PREFIX_BLOCK_SAMPLES

        frame_size = self._detector.frame_size
        block_bytes = PREFIX_BLOCK_SAMPLES * 4
        if len(block):
            block = np.asarray(block, dtype="<f4")
            self._pending_16k = np.concatenate((self._pending_16k, block)) if len(self._pending_16k) else block

        self._detector.restore(self._detector_state)
        try:
            # Feed at 512-sample frame granularity (32 ms) so closed segments emit immediately
            # without waiting for an 8192-sample prefix block boundary, while capturing O(1)
            # length-only checkpoints on every PREFIX_BLOCK_SAMPLES boundary.
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
                        stager = self._diarization_stager
                        if stager is not None:
                            staged_samples = len(self._block_hashes) * PREFIX_BLOCK_SAMPLES
                            spans_prov = self._build_staged_spans_provenance(staged_samples)
                            if spans_prov is None or not stager.append_block(
                                self._prefix_hash_buf,
                                origin_16k=self.origin_16k,
                                samples=staged_samples,
                                ownership_windows=self._ownership,
                                spans=spans_prov,
                                estimated_total_ms=self._last_total_end_ms,
                            ):
                                self.detach_diarization_stager()
                        self._prefix_hash_buf.clear()
                        self._latest_checkpoint = _PrefixCheckpoint(
                            block_count=len(self._block_hashes),
                            meter_code_count=len(self._meter.codes),
                            closed_range_count=len(self._closed_ranges),
                            ownership_windows=self._ownership,
                            offset_ms=self.placed_offset,
                            vad_input_stage=self._detector.input_stage,
                            max_samples=self._gate.max_samples,
                            detector_state=self._detector.snapshot(),
                            gate_state=self._gate.snapshot(),
                            open_start=self._open_start,
                        )
                elif self._diarization_stager is not None:
                    self.detach_diarization_stager()
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
            if self.unsupported or self.prefix_invalidated:
                self.detach_diarization_stager()
            return False
        self._last_spans = spans

        if self.sample_rate == RATE_16K:
            span = spans[0]
            self._last_total_end_ms = math.ceil((self.origin_16k + span["committed"]) * 1000 / RATE_16K)
            avail = span["committed"] - self._cursor_16k_raw
            if avail > 0:
                take = min(QUANTUM_16K, avail)
                block = read_wav_mono_f32(
                    dir_fd,
                    span["file"],
                    self._cursor_16k_raw,
                    take,
                    span["channels"],
                    raw_hasher=self._raw_hasher_16k,
                )
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
            if self.unsupported or self.prefix_invalidated:
                self.detach_diarization_stager()
            return False
        self._last_specs = specs
        max_committed_48k = self._last_max_committed_48k if specs else safe_end
        self._last_total_end_ms = math.ceil((self.origin_16k * 3 + max_committed_48k) * 1000 / RATE_48K)
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
        from .speech_pipeline import PREFIX_BLOCK_SAMPLES, SegmentationPrefix

        cp = self._latest_checkpoint
        if self.prefix_invalidated or self.unsupported or cp is None:
            return None
        return SegmentationPrefix(
            samples=cp.block_count * PREFIX_BLOCK_SAMPLES,
            block_hashes=tuple(self._block_hashes[: cp.block_count]),
            ownership_windows=cp.ownership_windows,
            offset_ms=cp.offset_ms,
            vad_input_stage=cp.vad_input_stage,
            max_samples=cp.max_samples,
            detector_state=cp.detector_state,
            gate_state=cp.gate_state,
            meter_codes=bytes(self._meter.codes[: cp.meter_code_count]),
            closed_ranges=tuple(dict(r) for r in self._closed_ranges[: cp.closed_range_count]),
            open_start=cp.open_start,
        )


class SessionFollower:
    """Multi-track earliest-session-time precompute coordinator (read-only on capture)."""

    def __init__(
        self,
        directory,
        model,
        model_identity,
        detector,
        *,
        on_cache_entry,
        record_trace: bool = False,
        diarization_options: dict | None = None,
        statvfs=None,
    ):
        self.directory = Path(directory)
        try:
            self.resolved_dir = str(self.directory.resolve())
        except OSError:
            self.resolved_dir = str(self.directory.absolute())
        self.model = model
        self.model_identity = model_identity
        self.detector = detector
        self.on_cache_entry = on_cache_entry
        self.diarization_options = diarization_options if isinstance(diarization_options, dict) else None
        self.statvfs = statvfs
        self.tracks: dict[str, TrackFollower] = {}
        self.session_ended = False
        self.segments_decoded = 0
        self._manifest_sig: tuple[int, int, int, int] | None = None
        self._manifest_loaded = False
        self._staged_track_id: str | None = None
        self._eligible_for_diarizer = False
        self._record_trace = bool(record_trace)
        self.schedule_trace: list[tuple[str, str, int]] = []  # Opt-in diagnostics/tests trace
        from . import recognition_cache

        self._cached_keys: set[str] = (
            set(recognition_cache.read(self.directory / recognition_cache.FILE, self.model_identity))
            if recognition_cache.is_valid_model_identity(self.model_identity)
            else set()
        )

    def _sync_diarization_staging(self, manifest_tracks: list) -> None:
        if self.diarization_options is None:
            return
        from .diarization_audio import TrackDiarizationStager

        valid_rows = []
        all_kinds = set()
        system_count = 0
        me_count = 0
        identity_counts: dict[str, int] = {}
        for row in manifest_tracks:
            if not isinstance(row, dict):
                continue
            tid = row.get("id")
            if not isinstance(tid, str) or not tid:
                continue
            kind = _track_kind(row, str(row.get("file", "")))
            all_kinds.add(kind)
            if kind == "system":
                system_count += 1
            elif kind in ("microphone", "local"):
                me_count += 1
            identity = str(row.get("sourceTrackId") or tid)
            identity_counts[identity] = identity_counts.get(identity, 0) + 1
            valid_rows.append((tid, kind, identity))

        self._eligible_for_diarizer = bool(
            system_count >= 1
            or (all_kinds & {"remote", "tab", "media", "application"})
            or (len(all_kinds) == 1 and me_count >= 1)
        )

        def is_single_source_eligible(tid: str, kind: str, identity: str) -> bool:
            tr = self.tracks.get(tid)
            if tr is None or tr.prefix_invalidated or tr.unsupported:
                return False
            if kind == "system":
                return system_count == 1
            if kind in {"remote", "tab", "media", "application"}:
                return identity_counts.get(identity, 0) == 1
            if kind in {"microphone", "local"}:
                return len(all_kinds) == 1 and me_count == 1
            return False

        if self._staged_track_id is not None:
            current_meta = next((item for item in valid_rows if item[0] == self._staged_track_id), None)
            current_tr = self.tracks.get(self._staged_track_id)
            if (
                current_meta is not None
                and current_tr is not None
                and current_tr._diarization_stager is not None
                and not current_tr._diarization_stager.aborted
                and is_single_source_eligible(*current_meta)
            ):
                return
            if current_tr is not None:
                current_tr.detach_diarization_stager()
            self._staged_track_id = None

        # Enforce G14 <= 1 group scratch file: choose the first single-source eligible track
        # in manifest order (matching diarization.group_tracks) that has not yet crossed its
        # first checkpoint.
        best_tid = None
        for tid, kind, identity in valid_rows:
            if not is_single_source_eligible(tid, kind, identity):
                continue
            tr = self.tracks[tid]
            if tr._emitted_16k > 0:
                continue
            best_tid = tid
            break
        if best_tid is not None:
            tr = self.tracks[best_tid]
            stager = TrackDiarizationStager(
                self.resolved_dir,
                tr.track_id,
                tr.primary_file,
                self.diarization_options,
                statvfs=self.statvfs,
            )
            if tr.attach_diarization_stager(stager):
                self._staged_track_id = best_tid

    def refresh(self) -> bool:
        sig = _stat_session_manifest(self.directory)
        if sig is not None and sig == self._manifest_sig and self._manifest_loaded:
            return True
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
        bounded_tracks = tracks[:64]
        for order, track in enumerate(bounded_tracks):
            if not isinstance(track, dict):
                continue
            track_id = track.get("id")
            if not isinstance(track_id, str) or not track_id:
                continue
            follower = self.tracks.get(track_id)
            if follower is None:
                follower = TrackFollower(
                    track_id, order, track, events, self.detector, session_ended=self.session_ended
                )
                self.tracks[track_id] = follower
            else:
                follower.order = order
                follower.update(track, events, self.session_ended)
        self._sync_diarization_staging(bounded_tracks)
        self._manifest_sig = sig
        self._manifest_loaded = True
        return True

    def step(self) -> bool:
        """Perform one bounded unit of work for the earliest-session-time ready track."""
        import numpy as np

        from . import recognition_cache
        from .speech_pipeline import transcribe_raw

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
                if did and self._record_trace:
                    self.schedule_trace.append((chosen.track_id, "advance", session_ms))
                return did
        finally:
            os.close(dir_fd)

        # Recognize outside any open capture file descriptor.
        ready = chosen.pop_ready_segment()
        if self._record_trace:
            self.schedule_trace.append((chosen.track_id, "recognize", ready.session_ms))
        samples = np.asarray(ready.samples, dtype="<f4")
        if len(samples) and np.isfinite(samples).all():
            key = recognition_cache.samples_key(samples)
            if key not in self._cached_keys:
                raw = transcribe_raw(self.model, samples)
                if recognition_cache.valid_entry(key, raw):
                    self._cached_keys.add(key)
                    self.segments_decoded += 1
                    self.on_cache_entry(dict(raw, key=key))
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

    def ready_for_diarizer_warm(self) -> bool:
        if self.diarization_options is None or not self._eligible_for_diarizer:
            return False
        if any(tr.has_ready_segment() for tr in self.tracks.values()):
            return False
        if any(tr.has_staged_diarization() for tr in self.tracks.values()):
            return True
        return any(tr._emitted_16k >= 3200 for tr in self.tracks.values())

    def staged_diarization_count(self) -> int:
        return sum(1 for tr in self.tracks.values() if tr.has_staged_diarization())

    def export_diarization_staged(self) -> dict:
        staged_map = {}
        for track in self.tracks.values():
            staged = track.export_diarization_staged()
            if staged is None:
                continue
            staged_map[(self.resolved_dir, track.track_id)] = staged
            if track.primary_file:
                staged_map[(self.resolved_dir, track.primary_file)] = staged
        self._staged_track_id = None
        return staged_map

    def close_diarization_staging(self) -> None:
        for track in self.tracks.values():
            track.detach_diarization_stager()
        self._staged_track_id = None


def run_follow_loop(
    model,
    bundle: Path,
    message: dict,
    emit,
    *,
    reader: StdinLineReader,
    detector,
    prefix_store: dict,
    diarization_store: dict | None = None,
    warm_diarizer=None,
):
    """Run read-only session following inside a verified speech worker until follow-stop/quit.

    Returns (next_command_or_None, detector). If a non-follow command was received
    or EOF/quit occurred, handles clean exit while keeping detector and prefix_store
    warm for an immediate post-Stop pipeline handoff.
    """
    from .diarization_audio import close_diarization_store
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
    prefix_store.clear()
    close_diarization_store(diarization_store)
    diarization_options = message.get("diarization") if isinstance(message.get("diarization"), dict) else None
    if diarization_options is None and warm_diarizer is not None:
        warm_diarizer.close()

    def on_cache(entry):
        emit("follow-cache", directory=directory, cacheEntry=entry)

    follower = SessionFollower(
        directory,
        model,
        model_identity,
        detector,
        on_cache_entry=on_cache,
        diarization_options=diarization_options,
    )
    idle_notified = False
    warm_attempted = False

    def cleanup_abort():
        follower.close_diarization_staging()
        close_diarization_store(diarization_store)
        if warm_diarizer is not None:
            warm_diarizer.close()

    def maybe_prewarm_diarizer():
        nonlocal warm_attempted
        if (
            warm_attempted
            or warm_diarizer is None
            or diarization_options is None
            or not follower.ready_for_diarizer_warm()
        ):
            return
        warm_attempted = True
        warm_diarizer.warm(diarization_options)

    def finish_follow_stop(cmd):
        drain = cmd.get("drain", True) is not False
        if drain:
            import time

            deadline = time.monotonic() + 2.0
            try:
                follower.refresh()
                while time.monotonic() < deadline:
                    if not any(tr.has_ready_segment() for tr in follower.tracks.values()):
                        break
                    if not follower.step():
                        break
            except (OSError, ValueError):
                pass
            maybe_prewarm_diarizer()
            exported = follower.export_prefixes()
            prefix_store.clear()
            prefix_store.update(exported)
            staged_count = 0
            if diarization_store is not None:
                exported_diar = follower.export_diarization_staged()
                diarization_store.update(exported_diar)
                staged_count = len({id(v) for v in exported_diar.values()})
            else:
                follower.close_diarization_staging()
        else:
            cleanup_abort()
            exported = {}
            prefix_store.clear()
            staged_count = 0
        emit(
            "follow-stopped",
            directory=directory,
            segments=follower.segments_decoded,
            prefixes=len(exported),
            diarizationStaged=staged_count,
            diarizerWarmed=bool(warm_diarizer is not None and warm_diarizer.model is not None),
        )
        return "continue", detector

    try:
        while True:
            # Check for stop/quit/preempt commands between every bounded step.
            line = reader.try_readline(0.0)
            if line is not None:
                if not line:
                    cleanup_abort()
                    return None, detector
                cmd = json.loads(line)
                cmd_type = cmd.get("type")
                if cmd_type == "follow-stop":
                    return finish_follow_stop(cmd)
                if cmd_type == "quit":
                    cleanup_abort()
                    return "quit", detector
                cleanup_abort()
                raise ValueError(f"Unexpected command during follow mode: {cmd_type}")

            try:
                follower.refresh()
                worked = follower.step()
            except (OSError, ValueError):
                worked = False

            if worked:
                idle_notified = False
                continue

            # When caught up with ASR work, pre-verify/pre-warm Nemotron once at background priority.
            maybe_prewarm_diarizer()

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
                    diarizationStaged=follower.staged_diarization_count(),
                    diarizerWarmed=bool(warm_diarizer is not None and warm_diarizer.model is not None),
                )
                idle_notified = True

            # Sleep reactively on stdin until either a command arrives or poll_s elapses.
            line = reader.try_readline(poll_s)
            if line is not None:
                if not line:
                    cleanup_abort()
                    return None, detector
                cmd = json.loads(line)
                cmd_type = cmd.get("type")
                if cmd_type == "follow-stop":
                    return finish_follow_stop(cmd)
                if cmd_type == "quit":
                    cleanup_abort()
                    return "quit", detector
                cleanup_abort()
                raise ValueError(f"Unexpected command during follow mode: {cmd_type}")
    except BaseException:
        cleanup_abort()
        raise
