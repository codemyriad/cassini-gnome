# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §4.3/§4.4: the timeline contract and the bounded mixdown into one mono Opus stream.

Timeline contract (§4.3), as GNOME reads session.json:

- A session.json track is a logical track. Its spans are its `spans` list when it
  has one (rotated files), otherwise the track entry itself is its only span.
  Each span is read through track_source (WAV or compacted Opus, §5), confined to
  the session folder; its length is the complete frames on disk (G25), never a
  header or a session.json count.
- startOffsetMs places a span's first sample on the session timeline. It is the
  only placement source and is applied exactly once (G1, G2). A null offset is
  mixed at 0 and reported (offsetsAssumedZero, D19).
- Gaps inside a span's PCM (gapFrames, `gapMaterialized`) are already samples and
  are never inserted again. Time between spans, or before a late joiner, is not in
  any PCM: it is mixed as explicit silence (G8, G10).
- Clock anchors (`clockAnchors`: [[sampleIndex, monotonicNs], ...], sampleIndex in
  the span's own source frames, ns against the session's monotonic origin) map
  samples to time by piecewise-linear interpolation, extrapolated with the nearest
  valid slope. Placement stays startOffsetMs: the anchors give the time *elapsed*
  since the span's first sample, A(i) - A(0), so the offset is never applied twice.
  A segment is valid when both coordinates increase and its rate is within 1 % of
  the nominal rate; with fewer than two valid anchors the nominal rate applies.
  GNOME's capture writes no anchors yet; this format is provisional (§10 REPLIES).

Mixdown (§4.4): each span is decoded to mono 48 kHz float by the export's reader
(export_audio.track_pcm: one GStreamer audioconvert/audioresample path for WAV and
Opus spans; stereo is averaged to mono), placed by its mapping, and summed with no
1/N normalisation in chunks of at most one second (G23); only a second or two per
source is ever held. Limiter is export's limited_pcm (0.95, 5 ms lookahead, 50 ms
release), vectorized; the encoder is export's D8 path (export_audio.encode_blocks).
"""

import array
import math
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .. import track_source
from ..export_audio import check_cancel

RATE = 48000
CHUNK = RATE  # <= 1 s per mixed block (G23)
FRAME = 960  # one 20 ms Opus frame: the §4.4 duration tolerance and the §9 marker tolerance
MAX_SLOPE_ERROR = 0.01
MAX_OFFSET_MS = 86400000


class EmptyRecording(ValueError):
    """No span has any audio frames."""


@dataclass(frozen=True)
class Span:
    track: str  # session.json track id
    path: Path  # the span's file, inside the session folder
    info: track_source.SpanInfo  # on-disk length and format (G25)
    start_ms: float
    anchors: tuple = ()
    offset_assumed: bool = False

    @property
    def length(self):
        """Mono 48 kHz frames the decoder yields for this span."""
        info = self.info
        if info.decoded_rate == RATE:
            return info.decoded_frames
        return round(info.frames * RATE / info.sample_rate)


def valid_anchors(anchors, rate):
    """[(sampleIndex, seconds)] of the usable anchors, in order (see module notes)."""
    points = []
    for value in anchors if isinstance(anchors, (list, tuple)) else ():
        if (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and all(type(v) is int and 0 <= v <= 2**63 for v in value)
        ):
            points.append((value[0], value[1]))
    points.sort()
    kept = []
    for index, ns in points:
        if kept:
            last_index, last_ns = kept[-1]
            if index <= last_index or ns <= last_ns:
                continue
            slope = ((ns - last_ns) / 1e9) / ((index - last_index) / rate)
            if abs(slope - 1) > MAX_SLOPE_ERROR:
                continue
        kept.append((index, ns))
    return [(index, ns / 1e9) for index, ns in kept]


def _linear(x, xs, ys):
    """Piecewise-linear through (xs, ys), extrapolated with the first/last segment's slope."""
    import numpy as np

    y = np.interp(x, xs, ys)
    first = (ys[1] - ys[0]) / (xs[1] - xs[0])
    last = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
    y = np.where(x < xs[0], ys[0] + (x - xs[0]) * first, y)
    return np.where(x > xs[-1], ys[-1] + (x - xs[-1]) * last, y)


class Mapping:
    """48 kHz decoded frame j of one span <-> output (timeline) sample position."""

    def __init__(self, span):
        import numpy as np

        rate = span.info.sample_rate
        origin = span.start_ms * RATE / 1000
        knots = valid_anchors(span.anchors, rate)
        if len(knots) < 2:
            self.knots = None
            self.shift = round(origin)
            return
        j = np.array([index * RATE / rate for index, _ in knots], dtype=np.float64)
        seconds = np.array([t for _, t in knots], dtype=np.float64)
        at_zero = float(_linear(np.array([0.0]), j, seconds)[0])
        self.knots = j, origin + (seconds - at_zero) * RATE

    def position(self, j):
        import numpy as np

        if self.knots is None:
            return np.asarray(j, dtype=np.float64) + self.shift
        return _linear(np.asarray(j, dtype=np.float64), *self.knots)

    def source(self, positions):
        import numpy as np

        if self.knots is None:
            return np.asarray(positions, dtype=np.float64) - self.shift
        j, p = self.knots
        return _linear(np.asarray(positions, dtype=np.float64), p, j)

    def end(self, length):
        return round(float(self.position([length])[0]))


@dataclass(frozen=True)
class Timeline:
    spans: list  # Span records with audio
    empty: list  # ids of tracks that recorded zero samples (G27)
    tracks: list  # x["codemyriad.io"].tracks: the track/span map as read from disk


SPAN_COPY = ("codec", "decodedRate", "sourceFile", "clockAnchors")


def session_spans(directory, manifest):
    """The §4.3 timeline of a stopped session.

    Raises ValueError for a span whose file is missing, unsafe or damaged: a
    meeting is never published without audio it expects.
    """
    tracks = manifest.get("tracks") if isinstance(manifest, dict) else None
    if not isinstance(tracks, list) or len(tracks) > 64:
        raise ValueError("invalid-session-tracks")
    spans, empty, records = [], [], []
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("id"), str):
            raise ValueError("invalid-session-track")
        values = track["spans"] if isinstance(track.get("spans"), list) else [track]
        found, map_spans = [], []
        for value in values:
            info = track_source.inspect(directory, value)
            offset = value.get("startOffsetMs")
            if offset is not None and (
                type(offset) not in (int, float) or not math.isfinite(offset) or not 0 <= offset <= MAX_OFFSET_MS
            ):
                raise ValueError("invalid-start-offset")
            record = dict(
                file=info.relative,
                sampleRate=info.sample_rate,
                channels=info.channels,
                frames=info.frames,
                startOffsetMs=offset,
            )
            if value.get("gapMaterialized") is True or (type(value.get("gapFrames")) is int and value["gapFrames"] > 0):
                record["gapMaterialized"] = True
            record.update({key: value[key] for key in SPAN_COPY if key in value})
            map_spans.append(record)
            if not info.frames:
                continue
            found.append(
                Span(
                    track=track["id"],
                    path=Path(directory).joinpath(*info.parts),
                    info=info,
                    start_ms=0.0 if offset is None else float(offset),
                    anchors=tuple(tuple(a) for a in value.get("clockAnchors", ()) if isinstance(a, list)),
                    offset_assumed=offset is None,
                )
            )
        if map_spans:
            entry = dict(id=track["id"])
            entry.update({key: track[key] for key in ("kind", "sourceTrackId") if key in track})
            records.append(dict(entry, spans=map_spans))
        if found:
            spans.extend(found)
        else:
            empty.append(track["id"])
    return Timeline(spans, empty, records)


def total_samples(spans):
    """Published duration in 48 kHz samples: the maximum mapped end over all spans (§4.4)."""
    return max((Mapping(span).end(span.length) for span in spans), default=0)


class Reader:
    """Sequential mono 48 kHz samples of one span; zeros outside [0, length)."""

    def __init__(self, span, cancel=None, blocks=None):
        import numpy as np

        from ..export_audio import track_pcm

        info = span.info
        track = SimpleNamespace(
            path=span.path,
            codec=info.codec,
            pcm_frames=info.decoded_frames if info.codec == "opus" else info.frames,
            pcm_rate=info.decoded_rate if info.codec == "opus" else info.sample_rate,
            channels=info.channels,
            start_offset_ms=0,
        )
        self.np = np
        self.length = span.length
        self.blocks = blocks if blocks is not None else track_pcm(track, cancel)
        self.base = 0  # index of buffer[0]
        self.buffer = np.zeros(0, dtype=np.float64)
        self.done = False

    def _fill(self, end):
        np = self.np
        parts = [self.buffer]
        have = self.base + len(self.buffer)
        while have < end and not self.done:
            block = next(self.blocks, None)
            if block is None:
                self.done = True
                break
            values = np.frombuffer(block, dtype=np.float32).astype(np.float64)
            parts.append(values)
            have += len(values)
        if len(parts) > 1:
            self.buffer = np.concatenate(parts)

    def get(self, start, end):
        """Samples [start, end), start >= every earlier start."""
        np = self.np
        if start < self.base:
            raise ValueError("mixdown reader moved backwards")
        self._fill(min(end, self.length))
        out = np.zeros(max(0, end - start), dtype=np.float64)
        lo, hi = max(start, 0), min(end, self.length, self.base + len(self.buffer))
        if lo < hi:
            out[lo - start : hi - start] = self.buffer[lo - self.base : hi - self.base]
        return out

    def release(self, before):
        if before > self.base:
            drop = min(before - self.base, len(self.buffer))
            self.buffer = self.buffer[drop:]
            self.base += drop

    def close(self):
        """Stop the decoder and drop the buffer; later reads are silence. Idempotent."""
        self.done = True
        self.buffer = self.np.zeros(0, dtype=self.np.float64)
        self.base = self.length
        close = getattr(self.blocks, "close", None)
        if close:
            close()


def mixed(spans, total, cancel=None, readers=None):
    """Mono float64 blocks of at most CHUNK samples covering [0, total)."""
    import numpy as np

    readers = readers or [Reader(span, cancel) for span in spans]
    mappings = [Mapping(span) for span in spans]
    try:
        for start in range(0, total, CHUNK):
            check_cancel(cancel)
            count = min(CHUNK, total - start)
            out = np.zeros(count, dtype=np.float64)
            for reader, mapping in zip(readers, mappings, strict=True):
                if mapping.knots is None:
                    first = start - mapping.shift
                    lo, hi = max(first, 0), min(first + count, reader.length)
                    if lo < hi:
                        out[lo - first : hi - first] += reader.get(lo, hi)
                        reader.release(hi)
                    if first + count >= reader.length:
                        reader.close()  # the span ended: its pipeline, fd and buffer go now
                    continue
                if reader.done and not len(reader.buffer):
                    continue
                j = mapping.source(np.arange(start, start + count + 1, dtype=np.float64))
                ended = j[-1] > reader.length - 1
                j = j[:-1]
                inside = (j >= 0) & (j <= reader.length - 1)
                if not inside.any():
                    if ended:
                        reader.close()
                    continue
                j = j[inside]
                low = np.floor(j).astype(np.int64)
                weight = j - low
                first = int(low.min())
                segment = reader.get(first, int(low.max()) + 2)
                index = low - first
                out[inside] += segment[index] * (1 - weight) + segment[index + 1] * weight
                reader.release(int(low.max()))
                if ended:
                    reader.close()
            yield out
    finally:
        for reader in readers:
            reader.close()


def sliding_max(values, width):
    """max(values[i:i+width]) for every full window (van Herk/Gil-Werman, O(n))."""
    import numpy as np

    n = len(values)
    padded = np.concatenate([values, np.zeros((-n) % width)])
    blocks = padded.reshape(-1, width)
    prefix = np.maximum.accumulate(blocks, axis=1).ravel()
    suffix = np.maximum.accumulate(blocks[:, ::-1], axis=1)[:, ::-1].ravel()
    index = np.arange(n - width + 1)
    return np.maximum(suffix[index], prefix[index + width - 1])


class Limiter:
    """export_audio.limited_pcm, vectorized, with the same output.

    Sample n is output `lookahead` samples late, scaled by g(n + lookahead), where
    g starts updating once `lookahead` samples are pending:
    g(n) = min(need(n), g(n-1) + (1 - g(n-1)) / release) and need(n) =
    min(1, limit / max|x[n-lookahead..n]|). With h = 1 - g that is
    h(n) = max(1 - need(n), a * h(n-1)), a = 1 - 1/release: a decaying running
    maximum, computed per block in the log domain.
    """

    def __init__(self, *, limit=0.95, lookahead=240, release=2400):
        import numpy as np

        self.np = np
        self.limit, self.lookahead = limit, lookahead
        self.log_decay = math.log1p(-1 / release)
        self.history = np.zeros(lookahead)  # |x| of the last `lookahead` samples
        self.pending = np.zeros(0)  # delay line
        self.count = 0
        self.attenuation = 0.0  # h = 1 - gain

    def process(self, block):
        np = self.np
        block = np.asarray(block, dtype=np.float64)
        if not np.isfinite(block).all():
            raise ValueError("Recording contains non-finite PCM")
        n = len(block)
        if not n:
            return np.zeros(0)
        magnitude = np.concatenate([self.history, np.abs(block)])
        self.history = magnitude[-self.lookahead :]
        first = max(0, self.lookahead - self.count)  # the first sample here that updates the gain
        self.count += n
        delay = np.concatenate([self.pending, block])
        if first >= n:
            self.pending = delay
            return np.zeros(0)
        steps = np.arange(n - first, dtype=np.float64) * self.log_decay
        if magnitude.max() <= self.limit:
            # No peak reaches the ceiling: need = 1 everywhere, so h only decays (the usual case).
            attenuation = self.attenuation * np.exp(steps + self.log_decay)
        else:
            peaks = sliding_max(magnitude, self.lookahead + 1)
            with np.errstate(divide="ignore"):
                need = np.where(peaks > 0, np.minimum(1.0, self.limit / np.where(peaks > 0, peaks, 1)), 1.0)
                terms = np.log(1 - need[first:]) - steps
                start = math.log(self.attenuation) + self.log_decay if self.attenuation > 0 else -math.inf
            running = np.maximum(np.maximum.accumulate(terms), start)
            attenuation = np.exp(running + steps)
        self.attenuation = float(attenuation[-1])
        out = delay[: len(attenuation)] * (1 - attenuation)
        self.pending = delay[len(attenuation) :]
        return out

    def flush(self):
        if not len(self.pending):
            return self.np.zeros(0)
        return self.process(self.np.zeros(self.lookahead))


def limited(blocks, limiter=None):
    limiter = limiter or Limiter()
    for block in blocks:
        out = limiter.process(block)
        if len(out):
            yield out
    tail = limiter.flush()
    if len(tail):
        yield tail


def encoder_blocks(blocks):
    """float64 numpy blocks -> array.array("f"), the encoder's input type."""
    for block in blocks:
        yield array.array("f", block.astype("<f4").tobytes())


def encode_choice(spans):
    """D8 encode settings for these sources, chosen as export chooses them."""
    from ..export_model import encode_choice

    return encode_choice(
        [
            dict(
                codec="pcm",
                sampleRate=s.info.sample_rate,
                speechBitrateBps=None,
                empty=s.info.frames < s.info.sample_rate,
            )
            for s in spans
        ]
    )


def encode(spans, output, choice, cancel=None):
    """Mix, limit and encode `spans` into the Ogg Opus file `output`; returns the expected samples."""
    from ..export_audio import encode_blocks

    total = total_samples(spans)
    if total <= 0:
        raise EmptyRecording("Meeting has no complete audio frames")
    source = mixed(spans, total, cancel)
    try:
        encode_blocks(encoder_blocks(limited(source)), output, choice, cancel)
    finally:
        source.close()  # stops every span's decoder pipeline now, not at garbage collection
    return total
