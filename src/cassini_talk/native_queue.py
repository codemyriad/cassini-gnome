# SPDX-License-Identifier: GPL-3.0-or-later
"""Bound native PCM before its C queue; never block the capture callback.

The queue is after conversion to PCM16 mono. Admission therefore knows exact
sample-frame counts, including callbacks dropped while its writer is stalled.
Only a short leaf lock protects metadata; no writer or capture lock is taken.
"""

import threading
from collections import deque


class NativeQueue:
    def __init__(self, rate=48000, *, capacity_frames=None):
        self.rate = rate
        self.capacity = rate * 10 if capacity_frames is None else capacity_frames
        if type(rate) is not int or rate <= 0 or type(self.capacity) is not int or self.capacity <= 0:
            raise ValueError("Invalid native queue capacity")
        self.last_end_ms = None
        self.dropped_frames = 0
        self._lock = threading.Lock()
        self._pending = deque()
        self._buffered = self._lost = 0
        self._cut = None

    def seal(self, at_ms):
        with self._lock:
            self._cut = at_ms

    def enter(self, stamp, frames, offset_ms):
        with self._lock:
            if self._cut is not None and offset_ms >= self._cut:
                return False  # After Stop: never admitted and never counted as loss.
            if frames <= 0:
                return False
            self.last_end_ms = offset_ms + frames * 1000 / self.rate
            if self._buffered + frames > self.capacity:
                self._lost += frames
                self.dropped_frames += frames
                return False
            self._pending.append((stamp, frames, self._lost))
            self._lost = 0
            self._buffered += frames
            return True

    def take(self, stamp):
        with self._lock:
            saved, frames, lost = self._pending.popleft()
            if saved != stamp:
                raise ValueError("Native queue reordered its admitted callbacks")
            return frames, lost

    def done(self, frames):
        with self._lock:
            self._buffered -= frames

    def tail(self):
        """After the pipeline stops: exact dropped or admitted-but-unwritten PCM."""
        with self._lock:
            lost = self._lost + sum(frames + before for _, frames, before in self._pending)
            self._pending.clear()
            self._lost = 0
            self._buffered = 0
            return lost

    def snapshot(self):
        with self._lock:
            return dict(buffered_frames=self._buffered, dropped_frames=self.dropped_frames)
