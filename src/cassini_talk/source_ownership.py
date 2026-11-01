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
        return tuple(windows)
    result = []
    for a, b in windows:
        for left, right in ownership:
            low = max(a, Fraction(str(left)))
            high = b if right is None else min(b, Fraction(str(right)))
            if low < high:
                result.append((low, high))
    return tuple(result)


def presence(track, ownership=None):
    """Span extents minus stored gap ranges, intersected with ownership windows.

    Never reads PCM: zero samples are not absence. A droppedFrames count has
    no position and is only reported. Without a ``gaps`` list (older writers)
    or after an overflow marker, the time is unknown. Returns None when the
    track has no known start offset.
    """
    from .source_spans import bounds

    if track.get("startOffsetMs", track.get("offsetMs")) is None:
        return None
    # Rotated/compacted span rows inherit the logical track's fields.
    logical = {key: value for key, value in track.items() if key != "spans"}
    extents = sorted(bounds(dict(logical, **span), display=True) for span in track.get("spans") or [{}])
    gaps = track.get("gaps")
    dropped = track.get("droppedFrames")
    dropped = dropped if type(dropped) is int and dropped > 0 else 0
    if not isinstance(gaps, list):
        return Presence((), _owned(extents, ownership), dropped, False)
    present, unknown, overflow = list(extents), [], False
    for gap in gaps:
        at = Fraction(str(gap["atMs"]))
        if gap["kind"] == "overflow":
            unknown = [(max(a, at), b) for a, b in present if b > at]
            present = [(a, min(b, at)) for a, b in present if a < at]
            overflow = True
            break
        present = _subtract(present, at, at + Fraction(str(gap["durationMs"])))
    return Presence(_owned(present, ownership), _owned(unknown, ownership), dropped, not overflow)


# D52.2: absorbs floating-point noise in stored times before taking the next sample.
EPSILON = Fraction(1, 1_000_000)


def samples(windows, rate=16000):
    """Half-open ms windows as [ceil(a*rate/1000 - eps), ceil(b*rate/1000 - eps)) sample ranges."""
    return [[math.ceil(Fraction(edge) * rate / 1000 - EPSILON) for edge in window] for window in windows]
