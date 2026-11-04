# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 track timing for the speech child, using the shared mixdown's exact map.

A logical source is mixed on its own, without the D8 limiter or other speakers.
The common FIR then runs once over that continuous 48 kHz stream, including
rotation boundaries and gaps. Ordinary v1 inputs and exact nominal clocks keep
D11's original seekable path and input bytes.
"""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

from .library_v2 import mixdown
from .placement import WORD_RATE, frame, word_ms


def _warped(value):
    rate = value.get("sampleRate")
    if type(rate) is not int or rate <= 0:
        return False
    anchors = value.get("clockAnchors")
    knots = mixdown.valid_anchors(anchors, rate)
    if len(knots) < 2:
        return False
    raw = {}
    for index, ns in sorted(
        tuple(point)
        for point in anchors
        if isinstance(point, (list, tuple))
        and len(point) == 2
        and all(type(part) is int and 0 <= part <= 2**63 for part in point)
    ):
        raw.setdefault((index, ns / 1e9), ns)
    first, seconds = knots[0]
    origin = raw[first, seconds]
    return any((raw[index, at] - origin) * rate != (index - first) * 1_000_000_000 for index, at in knots[1:])


def required(track):
    spans = track.get("spans")
    if isinstance(spans, list):
        return len(spans) > 1 or bool(spans and _warped(spans[0]))
    rate = track.get("sampleRate")
    return type(rate) is int and rate not in (16000, 48000) or _warped(track)


def timeline(track):
    """Inspect every physical file without following a span link out of the session."""
    path = Path(track["path"])
    directory = Path(track["sessionDirectory"]) if "sessionDirectory" in track else path.parent
    offset = track.get("offsetMs") or 0
    values = track.get("spans")
    if not isinstance(values, list):
        value = dict(
            file=path.relative_to(directory).as_posix(),
            startOffsetMs=offset,
            **{key: track[key] for key in ("sampleRate", "frames", "channels", "clockAnchors") if key in track},
        )
        if track.get("codec") == "opus":
            value.update(codec="opus", decodedRate=48000)
        values = [value]
    result = mixdown.session_spans(directory, dict(tracks=[dict(id=track["id"], spans=values)]))
    return result, offset


def end_ms(track):
    """The absolute end of the recognizer input, in the session ms of its word times (D55).

    The track's 16 kHz samples end at its placement plus their count, as
    speech_pipeline._source_end counts an unmapped track's.
    """
    spans, offset = _placed(track)
    return word_ms(offset, (mixdown.total_samples(spans) + 2) // 3)


def _local(windows, origin_ms):
    """Absolute session-ms windows on a timeline whose sample 0 is at `origin_ms`."""
    if windows is None:
        return None
    return tuple(
        (max(0, left - origin_ms), None if right is None else right - origin_ms)
        for left, right in windows
        if right is None or right > origin_ms
    )


def _placed(track):
    """The physical spans on the track's own recognizer timeline, and the track's offset.

    D55 places the track's sample 0 at session sample frame(offsetMs, 16000) and
    each physical span's first sample at frame(its startOffsetMs, 16000), the
    sample presence and Mac's recognizer place that file at. A span therefore
    starts at the whole local 16 kHz sample between the two, which is exactly the
    48 kHz frame three times that: the FIR's output sample n is centred on input
    frame 3n, so the span's first sample stays on its placement through the
    3:1 decimation, and the consumer adds the track's placement once.
    """
    result, offset = timeline(track)
    origin = frame(offset, WORD_RATE)
    # A span's own ownership windows (set when its logical track is unplaced) mask it before the FIR.
    owned = {
        span.get("file"): span.get("ownershipWindows") for span in track.get("spans") or () if isinstance(span, dict)
    }
    spans = []
    for span in result.spans:
        local = frame(span.start_ms, WORD_RATE) - origin
        if local < 0:
            raise ValueError("A physical span starts before its logical track")
        # local / 16 ms is a dyadic double, so mixdown's frame(start_ms, 48000) is exactly 3 * local.
        spans.append(replace(span, start_ms=local / 16, ownership=_local(owned.get(span.info.relative), origin / 16)))
    return spans, offset


def chunks(track):
    """Continuous, bounded 16 kHz source PCM; offset is applied by the consumer once."""
    import numpy as np

    from .speech_pipeline import resampled_stream

    spans, _offset = _placed(track)
    total = mixdown.total_samples(spans)
    with closing(mixdown.mixed(spans, total)) as source:
        yield from resampled_stream(np.asarray(block, dtype="<f4") for block in source)
