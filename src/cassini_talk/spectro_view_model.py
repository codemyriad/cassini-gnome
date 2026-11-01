# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 bounded display history and shared adaptive clock, independent of GTK."""

import bisect
import math
from array import array

SIDES = ("me", "others")
HOLE = 255
OFF, SILENT = 1, 2


class Delay:
    def __init__(self):
        self.value = 40.0
        self.previous = None
        self.times = array("d", [0]) * 320
        self.lags = array("d", [0]) * 320
        self.head = self.count = 0

    def arrived(self, now, earliest_slot):
        if self.count == len(self.times):
            self.head = (self.head + 1) % len(self.times)
            self.count -= 1
        index = (self.head + self.count) % len(self.times)
        self.times[index] = now
        self.lags[index] = min(1980, max(0, now - earliest_slot * 20))
        self.count += 1

    def advance(self, now):
        while self.count and self.times[self.head] < now - 3000:
            self.head = (self.head + 1) % len(self.times)
            self.count -= 1
        if self.count:
            values = sorted(self.lags[(self.head + i) % len(self.times)] for i in range(self.count))
            target = max(40, min(2000, values[math.ceil(0.95 * self.count) - 1] + 20))
        else:
            target = 40
        delta = 0 if self.previous is None else max(0, now - self.previous)
        self.previous = now
        if target > self.value:
            self.value = min(target, self.value + 0.5 * delta)
        else:
            self.value = max(target, self.value - 0.02 * delta)
        return self.value


class Pending:
    def __init__(self, capacity=256):
        self.capacity = capacity
        self.tags = array("q", [-1]) * capacity
        self.flags = bytearray(capacity)
        self.bands = bytearray(capacity * 32)
        self.overflow = 0

    def put(self, slot, flags, bands):
        index = slot % self.capacity
        if self.tags[index] not in (-1, slot):
            self.overflow += 1
        self.tags[index], self.flags[index] = slot, flags
        self.bands[index * 32 : (index + 1) * 32] = bands

    def take(self, slot):
        index = slot % self.capacity
        if self.tags[index] != slot:
            return None
        self.tags[index] = -1
        return self.flags[index], bytes(self.bands[index * 32 : (index + 1) * 32])


class DisplayModel:
    # The pill needs 52 pooled columns plus the 60 ms completion offset;
    # 212 analysis slots retain every source cell for both render sizes.
    CAPACITY = 212

    def __init__(self):
        self.delay = Delay()
        self.pending = {side: Pending() for side in SIDES}
        self.flags = {side: bytearray([HOLE]) * self.CAPACITY for side in SIDES}
        self.bands = {side: bytearray(self.CAPACITY * 32) for side in SIDES}
        self.last = None
        self.display_ms = -40
        self.late = 0
        self.selections = bytearray([3])
        self.selection_times = array("d", [0])
        self.warnings = [(0, False)]

    def set_events(self, events):
        selections = [(e["atMs"], e["me"], e["others"]) for e in events if e.get("type") == "selection"]
        selections = selections or [(0, True, True)]
        self.selection_times = array("d", (item[0] for item in selections))
        self.selections = bytearray(int(me) | (int(others) << 1) for _at, me, others in selections)

    def set_warning(self, now, silent):
        if self.warnings[-1][1] != silent:
            self.warnings.append((now, silent))
        while len(self.warnings) > 1 and self.warnings[1][0] < now - 5000:
            del self.warnings[0]

    def ingest(self, now, lanes):
        earliest = None
        for side in SIDES:
            for slot, flags, bands in lanes[side]:
                earliest = slot if earliest is None else min(earliest, slot)
                if self.last is not None and slot <= self.last:
                    self.late += 1
                else:
                    self.pending[side].put(slot, flags, bands)
        if earliest is not None:
            self.delay.arrived(now, earliest)

    def _state(self, side, slot):
        at = slot * 20
        index = bisect.bisect_right(self.selection_times, at) - 1
        selected = self.selections[index] & (1 if side == "me" else 2) if index >= 0 else True
        if not selected:
            return OFF
        if side == "me":
            warning = next((value for since, value in reversed(self.warnings) if since <= at), False)
            if warning:
                return SILENT
        return 0

    def advance(self, now):
        self.display_ms = now - self.delay.advance(now)
        end = math.floor(self.display_ms / 20)
        first = end - self.CAPACITY + 1 if self.last is None else self.last + 1
        first = max(first, end - self.CAPACITY + 1)
        if first > end:
            return ()
        for slot in range(first, end + 1):
            index = slot % self.CAPACITY
            for side in SIDES:
                state = self._state(side, slot)
                cell = self.pending[side].take(slot)
                if state & OFF:
                    self.flags[side][index] = OFF
                elif cell is None:
                    self.flags[side][index] = HOLE
                else:
                    flags, bands = cell
                    self.flags[side][index] = flags | state
                    self.bands[side][index * 32 : (index + 1) * 32] = bands
        self.last = end
        return range(first, end + 1)

    def cell(self, side, slot):
        if self.last is None or not self.last - self.CAPACITY < slot <= self.last:
            return HOLE, None
        index = slot % self.CAPACITY
        flags = self.flags[side][index]
        if flags == HOLE or flags & OFF:
            return flags, None
        return flags, bytes(self.bands[side][index * 32 : (index + 1) * 32])

    def pill_cell(self, side, slot):
        cells = [self.cell(side, slot * 4 + n) for n in range(4)]
        audio = [(flags, bands) for flags, bands in cells if bands is not None]
        if not audio:
            return (OFF if all(flags & OFF and flags != HOLE for flags, _ in cells) else HOLE), None
        pooled = [max(band[n] for _flags, band in audio) for n in range(32)]
        bands = bytes(max(pooled[n], pooled[n + 1]) for n in range(0, 32, 2))
        return (SILENT if any(flags & SILENT for flags, _ in audio) else 0), bands

    @property
    def latest_pill(self):
        return math.floor((self.display_ms - 60) / 80)
