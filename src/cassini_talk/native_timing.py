# SPDX-License-Identifier: GPL-3.0-or-later
"""Native v1.1: samples drive continuity; input host stamps detect real gaps."""

import logging
import math
from fractions import Fraction

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

    def map(self, host_ns, frames, offset_ms, *, writer_frames=0, inferred_frames=0):
        if type(host_ns) is not int or host_ns < 0 or type(frames) is not int or frames <= 0:
            raise ValueError("Invalid native input geometry")
        if type(offset_ms) not in (int, float) or not math.isfinite(offset_ms) or offset_ms < 0:
            raise ValueError("Invalid native first-buffer offset")
        if (
            any(type(n) is not int or n < 0 for n in (writer_frames, inferred_frames))
            or writer_frames
            and inferred_frames
        ):
            raise ValueError("Invalid native writer loss")
        self.writer_frames = writer_frames
        self.last_input_offset_ms = offset_ms
        gap = 0
        if self.previous_host_ns is None:
            self.anchor_ms = offset_ms
        else:
            # Integer arithmetic preserves the half-frame boundary. It uses
            # consecutive input host stamps, never callback arrival time or
            # accumulated lag since the session began.
            lag = (host_ns - self.previous_host_ns) * self.rate - self.previous_frames * NS
            self.writer_frames += min(inferred_frames, max(0, (lag + NS // 2) // NS))
            lag -= self.writer_frames * NS
            rounded = (lag + NS // 2) // NS
            if rounded >= self.rate:
                gap = rounded
            elif lag:
                self.diagnostic_buffers += 1
                self.max_skew_frames = max(self.max_skew_frames, abs(rounded))
                if self.diagnostic_buffers == 1:
                    LOG.debug("Native input clock skew below gap threshold: %g frames", lag / NS)
        self.frames += gap + self.writer_frames
        offset = self.anchor_ms + self.frames * 1000 / self.rate
        self.frames += frames
        self.previous_host_ns, self.previous_frames = host_ns, frames
        return offset, gap

    def tail(self, until_ms, *, resume_ms=None, writer_frames=None, inferred_frames=None):
        """D69.1: exact admitted callbacks, or a host/Stop-clipped estimate.

        Durations start at the last input's host end, never at the drifted
        sample-file end. Return (input gap, writer loss), appended at file time.
        """
        if self.previous_host_ns is None:
            return 0, 0
        if writer_frames is not None and inferred_frames is not None:
            raise ValueError("Known and inferred native loss overlap")

        def frames(ms):
            return math.floor(Fraction(str(ms)) * self.rate / 1000 + Fraction(1, 2))

        end = Fraction(str(self.last_input_offset_ms)) + Fraction(self.previous_frames * 1000, self.rate)
        until = max(0, frames(Fraction(str(until_ms)) - end))
        gap = 0
        if writer_frames is not None:
            lost = writer_frames
            if resume_ms is not None:
                gap = min(until, max(0, frames(Fraction(str(resume_ms)) - end) - lost))
        elif resume_ms is None:
            lost = until
        else:
            resume = frames(Fraction(str(resume_ms)) - end)
            start = max(0, resume - inferred_frames)
            gap = min(until, start)
            lost = max(0, min(until, resume) - start)
        gap = gap if gap >= self.rate else 0
        self.frames += gap + lost
        return gap, lost

    def flush_loss(self, session, identifier, admission, until_ms, *, inferred=False):
        """Stop streaming first, then preserve every remaining admitted callback."""
        known = admission.tail()
        if known:
            gap, lost = self.tail(until_ms, resume_ms=admission.last_end_ms, writer_frames=known)
            session.pad_native(identifier, gap_frames=gap, writer_frames=lost)
        if inferred:
            observed_end = admission.last_end_ms
            if observed_end is None:
                observed_end = self.last_input_offset_ms + self.previous_frames * 1000 / self.rate
            # Unobserved time begins after the latest observed callback,
            # including known drops. Never infer those exact frames twice.
            duration = max(Fraction(0), Fraction(str(until_ms)) - Fraction(str(observed_end)))
            lost = math.floor(duration * self.rate / 1000 + Fraction(1, 2))
            if lost:
                self.frames += lost
                session.pad_native(identifier, writer_frames=lost)
