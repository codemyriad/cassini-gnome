# SPDX-License-Identifier: GPL-3.0-or-later
"""D52/P8 half-open source placement; absence of events means unknown."""

import math
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction

from .session_events import read_events


def windows(kind, events):
    me = kind in ("microphone", "local")
    selected, source, begin = True, None, 0
    result = []

    def allowed():
        return selected and (me or source is None or source == ("system" if kind == "system" else "browser"))

    def append(left, right):
        if result and result[-1][1] == left:
            result[-1] = (result[-1][0], right)
        else:
            result.append((left, right))

    for event in read_events(events):
        if event["type"] not in ("selection", "othersSource"):
            continue
        at = event["atMs"]
        if allowed() and at > begin:
            append(begin, at)
        begin = at
        if event["type"] == "selection":
            selected = event["me" if me else "others"]
        elif not me:
            source = event["source"]
    if allowed():
        append(begin, None)
    return None if result == [(0, None)] else tuple(result)


class FrameMask:
    """Convert absolute half-open windows once, then mask bounded PCM blocks."""

    def __init__(self, intervals, rate, offset=0):
        self.ranges = None
        self.ends = ()
        if intervals is None:
            return
        if not isinstance(intervals, (list, tuple)) or len(intervals) > 1001:
            raise ValueError("Invalid source ownership windows")
        offset = Fraction(str(offset))
        self.ranges = []
        previous = 0
        for interval in intervals:
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise ValueError("Invalid source ownership window")
            left, right = interval
            if (
                type(left) not in (int, float)
                or not math.isfinite(left)
                or left < previous
                or right is not None
                and (type(right) not in (int, float) or not math.isfinite(right) or right <= left)
                or previous == math.inf
            ):
                raise ValueError("Invalid source ownership window")
            first = math.ceil((Fraction(str(left)) - offset) * rate / 1000)
            end = math.ceil((Fraction(str(right)) - offset) * rate / 1000) if right is not None else None
            self.ranges.append((first, end))
            previous = right if right is not None else math.inf
        self.ends = tuple(end if end is not None else math.inf for _begin, end in self.ranges)

    def sections(self, first, count):
        if self.ranges is None:
            return [(0, count)]
        result = []
        for index in range(bisect_right(self.ends, first), len(self.ranges)):
            begin, end = self.ranges[index]
            if begin >= first + count:
                break
            left = max(first, begin)
            right = first + count if end is None else min(first + count, end)
            if right > left:
                result.append((left - first, right - first))
        return result

    def pcm(self, data, first, width):
        count = len(data) // width
        sections = self.sections(first, count)
        if sections == [(0, count)]:
            return data
        output = bytearray(len(data))
        for left, right in sections:
            output[left * width : right * width] = data[left * width : right * width]
        return bytes(output)


@dataclass(frozen=True)
class Presence:
    """D52.2 half-open session-ms windows; unknown is neither present nor absent."""

    present: tuple
    unknown: tuple
    unpositioned_frames: int
    complete: bool


def _subtract(windows, left, right):
    result = []
    for a, b in windows:
        if right <= a or b <= left:
            result.append((a, b))
            continue
        if a < left:
            result.append((a, left))
        if right < b:
            result.append((right, b))
    return result


def _owned(windows, ownership):
    if ownership is None:
        return tuple((a, b) for a, b in windows if a < b)
    result = []
    for a, b in windows:
        for left, right in ownership:
            low = max(a, Fraction(str(left)))
            high = b if right is None else min(b, Fraction(str(right)))
            if low < high:
                result.append((low, high))
    return tuple(result)


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return Fraction(str(value))


def presence(track, ownership=None):
    """Span extents minus stored gap ranges, intersected with ownership windows.

    Never reads PCM: zero samples are not absence. A droppedFrames count has
    no position and is only reported. Without a ``gaps`` list (older writers),
    after an overflow marker, or from a malformed entry on, the time is unknown.
    Works on session.json tracks and on the published file's x["codemyriad.io"]
    tracks, whose offsets live only on their spans. Returns None when no span
    has a known start offset.
    """
    from .source_spans import bounds

    # Span rows inherit the logical track's fields; a span offset wins. A span
    # without an offset cannot be placed (display=False never assumes zero).
    logical = {key: value for key, value in track.items() if key != "spans"}
    rows = [dict(logical, **span) for span in track.get("spans") or [{}] if isinstance(span, dict)]
    placed = sorted(extent for row in rows if (extent := bounds(row)) is not None)
    if not placed:
        return None
    # A track rotated across files is one logical extent; empty spans add nothing.
    extents = []
    for a, b in placed:
        if a >= b:
            continue
        if extents and a <= extents[-1][1]:
            extents[-1] = (extents[-1][0], max(extents[-1][1], b))
        else:
            extents.append((a, b))
    gaps = track.get("gaps")
    dropped = track.get("droppedFrames")
    dropped = dropped if type(dropped) is int and dropped > 0 else 0
    if not isinstance(gaps, list):
        return Presence((), _owned(extents, ownership), dropped, False)
    present, unknown, complete = list(extents), [], True
    for gap in gaps:
        at = _time(gap.get("atMs")) if isinstance(gap, dict) else None
        duration = _time(gap.get("durationMs")) if isinstance(gap, dict) else None
        if at is not None and duration and gap.get("kind") != "overflow":
            present = _subtract(present, at, at + duration)
            continue
        # Overflow, or an entry this reader cannot place: unknown from its
        # start, or the whole track when even that is unusable.
        at = min((a for a, _ in extents), default=Fraction(0)) if at is None else at
        unknown = [(max(a, at), b) for a, b in present if b > at]
        present = [(a, min(b, at)) for a, b in present if a < at]
        complete = False
        break
    return Presence(_owned(present, ownership), _owned(unknown, ownership), dropped, complete)


# D52.2: absorbs floating-point noise in stored times before taking the next sample.
EPSILON = Fraction(1, 1_000_000)


def samples(windows, rate=16000):
    """Half-open ms windows as [ceil(a*rate/1000 - eps), ceil(b*rate/1000 - eps)) sample ranges."""
    return [[math.ceil(Fraction(edge) * rate / 1000 - EPSILON) for edge in window] for window in windows]
