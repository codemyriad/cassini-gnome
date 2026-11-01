# SPDX-License-Identifier: GPL-3.0-or-later
"""D52/P8 half-open source placement; absence of events means unknown."""

import math
from bisect import bisect_right
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
