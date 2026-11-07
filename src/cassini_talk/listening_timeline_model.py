# SPDX-License-Identifier: GPL-3.0-or-later
"""Widget-free timeline intervals and pixel snapping decisions."""

from heapq import heappop, heappush


def time_index(turns):
    """Latest-starting audible speaker at each boundary, including overlaps."""
    starts = {}
    boundaries = set()
    for index, turn in enumerate(turns):
        if turn.end_ms <= turn.start_ms:
            continue
        starts.setdefault(turn.start_ms, []).append((-turn.start_ms, -index, turn.end_ms, turn.speaker))
        boundaries.update((turn.start_ms, turn.end_ms))
    positions, speakers, active = [], [], []
    for at in sorted(boundaries):
        for value in starts.get(at, ()):
            heappush(active, value)
        while active and active[0][2] <= at:
            heappop(active)
        speaker = active[0][3] if active else None
        if not speakers or speakers[-1] != speaker:
            positions.append(at)
            speakers.append(speaker)
    return tuple(positions), tuple(speakers)


def snap_start(turns, x, left, width, duration):
    if not turns or width <= 0 or duration <= 0:
        return None
    closest = min(turns, key=lambda turn: abs(left + turn.start_ms * width / duration - x))
    distance = abs(round(left + closest.start_ms * width / duration) - x)
    return closest.start_ms if distance <= 6 else None
