# SPDX-License-Identifier: GPL-3.0-or-later
"""Native v1.1: samples drive continuity; input host stamps detect real gaps."""

import logging
import math

LOG = logging.getLogger(__name__)
NS = 1_000_000_000


class NativeTimeline:
    def __init__(self, rate):
        if type(rate) is not int or rate <= 0:
            raise ValueError("Invalid native sample rate")
        self.rate = rate
        self.frames = 0
        self.anchor_ms = None
        self.previous_host_ns = None
        self.previous_frames = 0
        self.diagnostic_buffers = 0
        self.max_skew_frames = 0

    def map(self, host_ns, frames, offset_ms):
        if type(host_ns) is not int or host_ns < 0 or type(frames) is not int or frames <= 0:
            raise ValueError("Invalid native input geometry")
        if type(offset_ms) not in (int, float) or not math.isfinite(offset_ms) or offset_ms < 0:
            raise ValueError("Invalid native first-buffer offset")
        gap = 0
        if self.previous_host_ns is None:
            self.anchor_ms = offset_ms
        else:
            # Integer arithmetic preserves the half-frame boundary. It uses
            # consecutive input host stamps, never callback arrival time or
            # accumulated lag since the session began.
            lag = (host_ns - self.previous_host_ns) * self.rate - self.previous_frames * NS
            rounded = (lag + NS // 2) // NS
            if rounded >= self.rate:
                gap = rounded
            elif lag:
                self.diagnostic_buffers += 1
                self.max_skew_frames = max(self.max_skew_frames, abs(rounded))
                if self.diagnostic_buffers == 1:
                    LOG.debug("Native input clock skew below gap threshold: %g frames", lag / NS)
        self.frames += gap
        offset = self.anchor_ms + self.frames * 1000 / self.rate
        self.frames += frames
        self.previous_host_ns, self.previous_frames = host_ns, frames
        return offset, gap
