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
    """The absolute source end, on exactly the same sample grid as publication."""
    result, _offset = timeline(track)
    return mixdown.total_samples(result.spans) * 1000 // mixdown.RATE


def chunks(track):
    """Continuous, bounded 16 kHz source PCM; offset is applied by the consumer once."""
    import numpy as np

    from .speech_pipeline import resampled_stream

    result, offset = timeline(track)
    spans = [replace(span, start_ms=span.start_ms - offset) for span in result.spans]
    if any(span.start_ms < 0 for span in spans):
        raise ValueError("A physical span starts before its logical track")
    total = mixdown.total_samples(spans)
    with closing(mixdown.mixed(spans, total)) as source:
        yield from resampled_stream(np.asarray(block, dtype="<f4") for block in source)
