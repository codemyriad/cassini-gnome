# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 rejoin on disk: stream/downmix/FIR, silence gaps and sum track spans.

Only the speech child imports this module. Long received audio never becomes a
WAV in /tmp or a concatenated in-memory PCM array.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .library_files import basename, regular
from .placement import frame
from .scratch_budget import BYTES_PER_MS, live_recordings, scratch_directory, shortfall, volume_usage
from .source_ownership import FrameMask

RATE_16K = 16000
RATE_48K = 48000
PREFIX_BLOCK_SAMPLES = 8192
_ZERO_CHUNK_BYTES = 65536


@dataclass(frozen=True)
class StagedSpanProvenance:
    """Immutable on-disk WAV span facts covering a staged 16 kHz diarization prefix."""

    file: str
    rate: int
    channels: int
    start_offset_ms: float
    ended: bool
    knots: tuple[tuple[float, ...], tuple[float, ...]] | None
    consumed_frames: int
    raw_sha256: str


class StagedDiarization:
    """Disposable fd-backed 16 kHz float32 prefix staged at background worker priority."""

    def __init__(
        self,
        *,
        session_dir: str,
        track_id: str,
        primary_file: str,
        stream,
        origin_16k: int,
        samples: int,
        ownership_windows,
        spans: tuple[StagedSpanProvenance, ...],
        sha256_state,
    ):
        self.session_dir = session_dir
        self.track_id = track_id
        self.primary_file = primary_file
        self.stream = stream
        self.origin_16k = origin_16k
        self.samples = samples
        self.ownership_windows = ownership_windows
        self._spans = spans
        self._sha256 = sha256_state
        self.closed = False

    @property
    def spans(self) -> tuple[StagedSpanProvenance, ...]:
        if (
            isinstance(self._spans, tuple)
            and len(self._spans) == 3
            and isinstance(self._spans[0], list)
            and type(self._spans[1]) is int
            and isinstance(self._spans[2], tuple)
        ):
            self._spans = (*self._spans[0][: self._spans[1]], *self._spans[2])
        return self._spans

    @property
    def total_frames(self) -> int:
        return self.origin_16k + self.samples

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            stream, self.stream = self.stream, None
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


class TrackDiarizationStager:
    """Incrementally writes verified 16 kHz <f4 prefix blocks into disposable scratch within G14."""

    def __init__(self, session_dir: str, track_id: str, primary_file: str, options: dict, *, statvfs=None):
        self.session_dir = session_dir
        self.track_id = track_id
        self.primary_file = primary_file
        self.scratch = options.get("scratch")
        self.live = live_recordings(options)
        self.statvfs = statvfs
        self._stream = None
        self._sha256 = None
        self._origin_16k: int | None = None
        self._samples = 0
        self._written_bytes = 0
        self._initial_free: int | None = None
        self._staged: StagedDiarization | None = None
        self.aborted = False

    def _effective_statvfs(self):
        base = self.statvfs or volume_usage
        if self._written_bytes <= 0 or self._initial_free is None:
            return base

        def adjusted(path):
            usage = base(path)
            free = usage.f_bavail * usage.f_frsize
            # Test doubles often return a constant statvfs snapshot that does not
            # reflect blocks written to our own TemporaryFile; subtract our bytes
            # when f_bavail stayed pinned to its pre-open value.
            if free == self._initial_free and usage.f_frsize > 0:
                rem = max(0, free - self._written_bytes)
                from types import SimpleNamespace

                return SimpleNamespace(
                    f_bavail=rem // usage.f_frsize,
                    f_frsize=usage.f_frsize,
                    f_blocks=usage.f_blocks,
                )
            return usage

        return adjusted

    def _fits_budget(self, needed_ms: int) -> bool:
        return (
            shortfall(
                needed_ms,
                scratch=self.scratch,
                statvfs=self._effective_statvfs(),
                recordings=self.live,
            )
            is None
        )

    def append_block(
        self,
        block_bytes: bytes | bytearray,
        *,
        origin_16k: int,
        samples: int,
        ownership_windows,
        spans: tuple[StagedSpanProvenance, ...],
        estimated_total_ms: int = 0,
    ) -> bool:
        if self.aborted:
            return False
        raw = bytes(block_bytes)
        block_samples = len(raw) // 4
        has_spans = (
            bool(spans[1] or spans[2])
            if (isinstance(spans, tuple) and len(spans) == 3 and isinstance(spans[0], list) and type(spans[1]) is int)
            else bool(spans)
        )
        if (
            len(raw) != PREFIX_BLOCK_SAMPLES * 4
            or origin_16k < 0
            or samples != self._samples + block_samples
            or not has_spans
        ):
            self.abort()
            return False
        import numpy as np

        arr = np.frombuffer(raw, dtype="<f4")
        if not np.isfinite(arr).all():
            self.abort()
            return False
        try:
            if self._stream is None:
                first_ms = math.ceil((origin_16k + block_samples) * 1000 / RATE_16K)
                if not self._fits_budget(max(first_ms, estimated_total_ms)):
                    self.abort()
                    return False
                directory = scratch_directory(self.scratch)
                usage = (self.statvfs or volume_usage)(directory)
                self._initial_free = usage.f_bavail * usage.f_frsize
                self._stream = tempfile.TemporaryFile(dir=directory)  # noqa: SIM115 — owned across checkpoints until post-Stop or close()
                self._sha256 = hashlib.sha256()
                self._origin_16k = origin_16k
                if origin_16k > 0:
                    zero_buf = b"\x00" * min(_ZERO_CHUNK_BYTES, origin_16k * 4)
                    remaining = origin_16k * 4
                    while remaining > 0:
                        chunk = zero_buf if remaining >= len(zero_buf) else zero_buf[:remaining]
                        self._stream.write(chunk)
                        self._sha256.update(chunk)
                        self._written_bytes += len(chunk)
                        remaining -= len(chunk)
            else:
                if origin_16k != self._origin_16k:
                    self.abort()
                    return False
                delta_ms = math.ceil(len(raw) / BYTES_PER_MS)
                if not self._fits_budget(delta_ms):
                    self.abort()
                    return False
            self._stream.write(raw)
            self._stream.flush()
            self._sha256.update(raw)
            self._written_bytes += len(raw)
            self._samples = samples
            self._staged = StagedDiarization(
                session_dir=self.session_dir,
                track_id=self.track_id,
                primary_file=self.primary_file,
                stream=self._stream,
                origin_16k=origin_16k,
                samples=samples,
                ownership_windows=ownership_windows,
                spans=spans,
                sha256_state=self._sha256.copy(),
            )
            return True
        except OSError:
            self.abort()
            return False

    def export(self) -> StagedDiarization | None:
        if self.aborted or self._staged is None or self._stream is None or self._samples <= 0:
            self.close()
            return None
        staged = self._staged
        self._staged = None
        self._stream = None
        self._sha256 = None
        return staged

    def abort(self) -> None:
        self.aborted = True
        self.close()

    def close(self) -> None:
        staged, self._staged = self._staged, None
        stream, self._stream = self._stream, None
        self._sha256 = None
        if staged is not None:
            staged.close()
        elif stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


def close_diarization_store(store: dict | None) -> None:
    if not store:
        return
    entries = list(store.values())
    store.clear()
    seen = set()
    for item in entries:
        if isinstance(item, StagedDiarization) and id(item) not in seen:
            seen.add(id(item))
            item.close()


def _track_lookup_keys(track: dict) -> tuple[str, list[tuple[str, str]]]:
    path = Path(track["path"])
    session_dir = Path(track["sessionDirectory"]) if "sessionDirectory" in track else path.parent
    try:
        resolved = str(session_dir.resolve())
    except OSError:
        resolved = str(session_dir.absolute())
    candidates = []
    if track.get("captureTrackId") is not None:
        candidates.append((resolved, str(track["captureTrackId"])))
    candidates.append((resolved, str(track["id"])))
    candidates.append((resolved, path.name))
    return resolved, candidates


def pop_staged_for_track(store: dict | None, track: dict) -> StagedDiarization | None:
    if not store:
        return None
    _resolved, candidates = _track_lookup_keys(track)
    found = None
    for key in candidates:
        candidate = store.get(key)
        if isinstance(candidate, StagedDiarization):
            found = candidate
            break
    if found is None:
        return None
    for key, value in list(store.items()):
        if value is found:
            store.pop(key, None)
    return found


def discard_staged_for_track(store: dict | None, track: dict) -> None:
    found = pop_staged_for_track(store, track)
    if found is not None:
        found.close()


def _verify_wav_pcm_prefix(
    dir_fd: int,
    filename: str,
    expected_rate: int,
    expected_channels: int,
    consumed_frames: int,
    expected_sha256: str,
    *,
    require_exact_frames: bool,
) -> int | None:
    """Verify WAV header geometry and SHA-256 over PCM payload [44, 44 + consumed_frames * align)."""
    from .session_follower import read_wav_committed

    safe_name = basename(filename)
    info = read_wav_committed(dir_fd, safe_name)
    if info is None:
        return None
    committed, rate, channels = info
    if rate != expected_rate or channels != expected_channels or committed < consumed_frames or consumed_frames < 0:
        return None
    if require_exact_frames and committed != consumed_frames:
        return None
    alignment = channels * 2
    remaining = consumed_frames * alignment
    digest = hashlib.sha256()
    buf = bytearray(262144)
    view = memoryview(buf)
    try:
        with regular(dir_fd, safe_name) as (stream, _stat):
            stream.seek(44)
            while remaining > 0:
                take = min(len(buf), remaining)
                got = stream.readinto(view[:take])
                if not got:
                    return None
                digest.update(view[:got])
                remaining -= got
    except (OSError, ValueError):
        return None
    if digest.hexdigest() != expected_sha256:
        return None
    return committed


class _TailSpanReader:
    """Sequential mono float64 reader starting directly at the tail boundary of one WAV span."""

    def __init__(self, filename: str, channels: int, length: int, start_frame: int):
        import numpy as np

        self.filename = basename(filename)
        self.channels = channels
        self.length = length
        self.read_cursor = max(0, min(length, start_frame))
        self.base = self.read_cursor
        self.buffer = np.empty(0, dtype=np.float64)

    def get(self, dir_fd: int, start: int, end: int):
        import numpy as np

        from .session_follower import read_wav_mono_f32

        if start < self.base:
            raise ValueError("Tail span reader moved backwards")
        target = min(end, self.length)
        if target > self.read_cursor:
            parts = [self.buffer] if len(self.buffer) else []
            while self.read_cursor < target:
                step = min(RATE_48K, target - self.read_cursor)
                block = read_wav_mono_f32(dir_fd, self.filename, self.read_cursor, step, self.channels)
                parts.append(block.astype(np.float64))
                self.read_cursor += step
            self.buffer = np.concatenate(parts) if len(parts) > 1 else parts[0]
        out = np.zeros(max(0, end - start), dtype=np.float64)
        lo = max(start, 0)
        hi = min(end, self.length, self.base + len(self.buffer))
        if lo < hi:
            out[lo - start : hi - start] = self.buffer[lo - self.base : hi - self.base]
        return out

    def release(self, before: int) -> None:
        if before > self.base:
            drop = min(before - self.base, len(self.buffer))
            self.buffer = self.buffer[drop:]
            self.base += drop


def _mapped_48k_tail_chunks(session_dir: Path, spans: list, mappings: list, start_16k: int, total_48k: int):
    """Yield 16 kHz <f4 unmasked tail blocks for [start_16k, end) without reading prefix WAV frames."""
    import numpy as np

    from .speech_pipeline import IncrementalResampler

    low_48k = max(0, start_16k * 3 - 48)
    resampler = IncrementalResampler()
    resampler.low = low_48k
    resampler.total = low_48k
    resampler.first = start_16k
    active_tail = []
    for span, mapping in zip(spans, mappings, strict=True):
        length = span.length
        if length <= 0:
            continue
        if mapping.knots is None:
            if low_48k - mapping.shift >= length:
                continue
        elif float(mapping.source(np.array([float(low_48k)]))[0]) > length - 1:
            continue
        active_tail.append((span, mapping))
    readers: list[_TailSpanReader | None] = [None] * len(active_tail)
    dir_fd = os.open(session_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for start in range(low_48k, total_48k, RATE_48K):
            count = min(RATE_48K, total_48k - start)
            out = np.zeros(count, dtype=np.float64)
            for idx, (span, mapping) in enumerate(active_tail):
                length = span.length
                if mapping.knots is None:
                    first = start - mapping.shift
                    lo, hi = max(first, 0), min(first + count, length)
                    if lo < hi:
                        reader = readers[idx]
                        if reader is None:
                            reader = readers[idx] = _TailSpanReader(span.info.relative, span.info.channels, length, lo)
                        out[lo - first : hi - first] += reader.get(dir_fd, lo, hi)
                        reader.release(hi)
                    continue
                j = mapping.source(np.arange(start, start + count, dtype=np.float64))
                inside = (j >= 0) & (j <= length - 1)
                if not inside.any():
                    continue
                j_in = j[inside]
                low = np.floor(j_in).astype(np.int64)
                weight = j_in - low
                first = int(low.min())
                reader = readers[idx]
                if reader is None:
                    reader = readers[idx] = _TailSpanReader(span.info.relative, span.info.channels, length, first)
                segment = reader.get(dir_fd, first, int(low.max()) + 2)
                index = low - first
                out[inside] += segment[index] * (1.0 - weight) + segment[index + 1] * weight
                reader.release(int(low.max()))
            yield from resampler.feed(out.astype("<f4"))
        yield from resampler.flush()
    finally:
        os.close(dir_fd)


def _verify_staged_and_plan_tail(track: dict, staged: StagedDiarization):
    """Strictly verify staged prefix against finalized track/audio/clock/ownership and return tail iterator."""
    import numpy as np

    from . import mapped_audio
    from .library_v2 import mixdown
    from .speech_pipeline import resampled_chunks

    if staged.closed or staged.stream is None or staged.samples <= 0 or staged.samples % PREFIX_BLOCK_SAMPLES != 0:
        return None
    try:
        file_info = os.fstat(staged.stream.fileno())
    except OSError:
        return None
    if file_info.st_size != staged.total_frames * 4:
        return None
    if track.get("silent") is True or track.get("codec") == "opus" or track.get("format") == "opus":
        return None
    offset = track.get("offsetMs", track.get("startOffsetMs")) or 0
    if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
        return None
    if frame(offset, RATE_16K) != staged.origin_16k:
        return None
    if any(isinstance(s, dict) and "ownershipWindows" in s for s in track.get("spans") or ()):
        return None
    try:
        before_mask = FrameMask(staged.ownership_windows, RATE_16K, staged.origin_16k / 16)
        after_mask = FrameMask(track.get("ownershipWindows"), RATE_16K, staged.origin_16k / 16)
    except ValueError:
        return None
    if before_mask.sections(0, staged.samples) != after_mask.sections(0, staged.samples):
        return None

    resolved_dir, _keys = _track_lookup_keys(track)
    if resolved_dir != staged.session_dir:
        return None
    session_dir = Path(track["sessionDirectory"]) if "sessionDirectory" in track else Path(track["path"]).parent
    s0 = staged.samples
    max_prefix_48k = (s0 - 1) * 3 + 48

    if not mapped_audio.required(track):
        if len(staged.spans) != 1:
            return None
        sp = staged.spans[0]
        if sp.rate not in (RATE_16K, RATE_48K) or sp.start_offset_ms != float(offset):
            return None
        if sp.knots is not None:
            if mapped_audio._warped(track):
                return None
            knots = mixdown.valid_anchors(track.get("clockAnchors"), sp.rate)
            if len(knots) < len(sp.knots[0]):
                return None
            j0 = knots[0][0] * RATE_48K / sp.rate
            j1 = knots[1][0] * RATE_48K / sp.rate
            s_0, s_1 = knots[0][1], knots[1][1]
            at_zero = s_0 + (0.0 - j0) * ((s_1 - s_0) / (j1 - j0))
            fj = tuple(idx * RATE_48K / sp.rate for idx, _ in knots[: len(sp.knots[0])])
            fp = tuple((t - at_zero) * RATE_48K for _, t in knots[: len(sp.knots[1])])
            if fj != sp.knots[0] or fp != sp.knots[1]:
                return None
            if len(knots) > len(sp.knots[0]) and max_prefix_48k > sp.knots[1][-1]:
                return None
        try:
            dir_fd = os.open(session_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            target_file = Path(track["path"]).name
            if basename(sp.file) != basename(target_file):
                return None
            committed = _verify_wav_pcm_prefix(
                dir_fd,
                target_file,
                sp.rate,
                sp.channels,
                sp.consumed_frames,
                sp.raw_sha256,
                require_exact_frames=sp.ended,
            )
        finally:
            os.close(dir_fd)
        if committed is None:
            return None
        if sp.rate == RATE_16K:
            if sp.consumed_frames < s0 or committed < s0:
                return None
        else:
            if not sp.ended and max_prefix_48k >= sp.consumed_frames:
                return None
            if (committed + 2) // 3 < s0:
                return None
        return resampled_chunks(track["path"], start=s0), after_mask

    try:
        spans, _placed_offset = mapped_audio._placed(track)
    except (OSError, ValueError):
        return None
    if len(spans) < len(staged.spans) or not staged.spans:
        return None
    if any(
        span.info.codec != "pcm"
        or span.info.sample_rate != RATE_48K
        or span.info.channels not in (1, 2)
        or span.ownership is not None
        for span in spans
    ):
        return None
    mappings = [mixdown.Mapping(span) for span in spans]
    total_48k = max((m.end(span.length) for span, m in zip(spans, mappings, strict=True)), default=0)
    if (total_48k + 2) // 3 < s0:
        return None

    try:
        dir_fd = os.open(session_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for sp, final_span, final_mapping in zip(
            staged.spans, spans[: len(staged.spans)], mappings[: len(staged.spans)], strict=True
        ):
            if final_span.info.relative != sp.file or sp.rate != RATE_48K:
                return None
            local_16k = frame(sp.start_offset_ms, RATE_16K) - staged.origin_16k
            if local_16k != round(final_span.start_ms * 16):
                return None
            if sp.knots is None:
                if final_mapping.knots is not None:
                    return None
                if not sp.ended and max_prefix_48k - final_mapping.shift >= sp.consumed_frames:
                    return None
            else:
                if final_mapping.knots is None:
                    return None
                fj, fp = final_mapping.knots
                if len(fj) < len(sp.knots[0]):
                    return None
                if not np.array_equal(fj[: len(sp.knots[0])], np.asarray(sp.knots[0], dtype=np.float64)):
                    return None
                if not np.array_equal(fp[: len(sp.knots[1])], np.asarray(sp.knots[1], dtype=np.float64)):
                    return None
                if len(fj) > len(sp.knots[0]) and max_prefix_48k > sp.knots[1][-1]:
                    return None
                if not sp.ended:
                    max_j = float(final_mapping.source(np.array([float(max_prefix_48k)]))[0])
                    if max_j >= 0.0 and int(math.floor(max_j)) + 2 > sp.consumed_frames:
                        return None
            committed = _verify_wav_pcm_prefix(
                dir_fd,
                sp.file,
                sp.rate,
                sp.channels,
                sp.consumed_frames,
                sp.raw_sha256,
                require_exact_frames=sp.ended,
            )
            if committed is None or committed != final_span.info.frames:
                return None

        for _final_span, final_mapping in zip(spans[len(staged.spans) :], mappings[len(staged.spans) :], strict=True):
            if final_mapping.knots is None:
                if final_mapping.shift <= max_prefix_48k:
                    return None
            else:
                if float(final_mapping.source(np.array([float(max_prefix_48k)]))[0]) >= 0.0:
                    return None
    finally:
        os.close(dir_fd)

    return _mapped_48k_tail_chunks(session_dir, spans, mappings, s0, total_48k), after_mask


@contextmanager
def rejoin(tracks, *, scratch=None, heartbeat=lambda: None):
    import numpy as np

    from .speech_pipeline import audio_chunks

    directory = scratch_directory(scratch)
    last_notice = time.monotonic()
    # A file descriptor avoids transporting paths to temporary audio; no
    # transcript/capture file is opened for writing by the worker.
    with tempfile.TemporaryFile(dir=directory) as stream:
        frames = 0
        for track in tracks:
            offset = track.get("offsetMs", track.get("startOffsetMs")) or 0
            if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
                raise ValueError("Invalid diarization track offset")
            cursor = frame(offset, 16000)  # D55
            with closing(audio_chunks(track)) as chunks:
                for block in chunks:
                    if not len(block):
                        continue
                    if len(block) > 16000 or block.ndim != 1 or not np.isfinite(block).all():
                        raise ValueError("Invalid diarization PCM block")
                    stream.seek(cursor * 4)
                    previous = stream.read(len(block) * 4)
                    mixed = np.asarray(block, dtype="<f4").copy()
                    if previous:
                        mixed[: len(previous) // 4] += np.frombuffer(previous, dtype="<f4")
                    stream.seek(cursor * 4)
                    stream.write(mixed.tobytes())
                    cursor += len(block)
                    frames = max(frames, cursor)
                    if time.monotonic() - last_notice >= 1:
                        heartbeat()
                        last_notice = time.monotonic()
        stream.flush()
        stream.seek(0)
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        yield stream, frames, fingerprint


class LowDiskFallbackError(Exception):
    """Raised when a staged prefix falls back to full rejoin and full rejoin exceeds scratch_budget."""

    def __init__(self, shortfall_info: dict):
        super().__init__("Not enough disk space")
        self.shortfall_info = shortfall_info


@contextmanager
def rejoin_with_staged(
    tracks,
    *,
    staged: StagedDiarization | None = None,
    scratch=None,
    heartbeat=lambda: None,
    check_full_budget=None,
):
    """Reuse verified staged 16 kHz prefix + append < 60 s tail, or fall back cleanly to full rejoin."""
    ready_staged = None
    if staged is not None and len(tracks) == 1:
        import numpy as np

        verified = None
        try:
            verified = _verify_staged_and_plan_tail(tracks[0], staged)
            if verified is not None:
                tail_iter, after_mask = verified
                last_notice = time.monotonic()
                local_cursor = staged.samples
                tail_frames = 0
                digest = staged._sha256.copy()
                staged.stream.seek(staged.total_frames * 4)
                with closing(tail_iter) as chunks:
                    for block in chunks:
                        if not len(block):
                            continue
                        if len(block) > 16000 or block.ndim != 1 or not np.isfinite(block).all():
                            raise ValueError("Invalid diarization PCM block")
                        sections = after_mask.sections(local_cursor, len(block))
                        if sections != [(0, len(block))]:
                            selected = np.zeros_like(block)
                            for left, right in sections:
                                selected[left:right] = block[left:right]
                            block = selected
                        raw = np.asarray(block, dtype="<f4").tobytes()
                        staged.stream.write(raw)
                        digest.update(raw)
                        local_cursor += len(block)
                        tail_frames += len(block)
                        if time.monotonic() - last_notice >= 1:
                            heartbeat()
                            last_notice = time.monotonic()
                frames = staged.origin_16k + local_cursor
                staged.stream.flush()
                staged.stream.seek(0)
                fingerprint = digest.hexdigest()
                ready_staged = (staged.stream, frames, fingerprint, True, tail_frames)
        except (OSError, ValueError):
            if verified is not None:
                with contextlib.suppress(Exception):
                    verified[0].close()
            ready_staged = None

        if ready_staged is not None:
            try:
                yield ready_staged
            finally:
                staged.close()
            return

        staged.close()
        staged = None
        if check_full_budget is not None and (short := check_full_budget()):
            raise LowDiskFallbackError(short)
    elif staged is not None:
        staged.close()
        staged = None
        if check_full_budget is not None and (short := check_full_budget()):
            raise LowDiskFallbackError(short)

    with rejoin(tracks, scratch=scratch, heartbeat=heartbeat) as (stream, frames, fingerprint):
        yield stream, frames, fingerprint, False, frames
