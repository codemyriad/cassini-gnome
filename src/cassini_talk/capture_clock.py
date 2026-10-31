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


def mapped_end_sample(frames, rate, offset_ms, anchors, *, decoded_frames=None):
    """Cheap scalar projection of Mapping.end, without importing NumPy in GTK.

    Use the core's exact knot validation and 48 kHz grid. Playback/recognition
    sample positions still use Mapping itself on their worker threads.
    """
    from .library_v2.mixdown import valid_anchors

    points = valid_anchors(anchors, rate)
    length = round(frames * 48000 / rate) if decoded_frames is None else decoded_frames
    origin = float(offset_ms or 0) * 48
    if len(points) < 2:
        return length + round(origin)
    xs = [index * 48000 / rate for index, _ in points]
    seconds = [at for _, at in points]

    def linear(x, ys):
        from bisect import bisect_right

        right = min(max(1, bisect_right(xs, x)), len(xs) - 1)
        left = right - 1
        slope = (ys[right] - ys[left]) / (xs[right] - xs[left])
        return ys[left] + (x - xs[left]) * slope

    zero = linear(0, seconds)
    timeline = [origin + (at - zero) * 48000 for at in seconds]
    return round(linear(length, timeline))
