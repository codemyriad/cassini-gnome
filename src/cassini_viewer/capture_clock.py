# SPDX-License-Identifier: GPL-3.0-or-later
"""Sparse native sample/host-clock observations for the S-12 elapsed-time map.

Keep the first sample stamp, one point per source minute and the latest buffer's
first-sample stamp. Arrival time and inferred buffer ends are never observations.
Invalid/backwards observations are ignored; saved PCM is never changed here.
"""


class ClockAnchors:
    def __init__(self, rate, origin_ns):
        if type(rate) is not int or not 8000 <= rate <= 192000 or type(origin_ns) is not int or origin_ns < 0:
            raise ValueError("Invalid native clock geometry")
        self.rate = rate
        self.origin_ns = origin_ns
        self.fixed = []
        self.latest = None

    def add(self, sample_index, host_ns):
        if type(sample_index) is not int or type(host_ns) is not int or sample_index < 0:
            return
        relative = host_ns - self.origin_ns
        if not 0 <= relative <= 2**63:
            return
        point = sample_index, relative
        if self.latest is not None and (point[0] <= self.latest[0] or point[1] <= self.latest[1]):
            return
        if not self.fixed or sample_index - self.fixed[-1][0] >= self.rate * 60:
            self.fixed.append(point)
        self.latest = point

    def values(self):
        values = list(self.fixed)
        if self.latest is not None and self.latest != self.fixed[-1]:
            values.append(self.latest)
        return [list(point) for point in values]
