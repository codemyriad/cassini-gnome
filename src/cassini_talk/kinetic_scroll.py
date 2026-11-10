# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Relative kinetic motion, independent of a virtual list's pixel origin."""

import math
from dataclasses import dataclass


@dataclass
class KineticMotion:
    velocity: float
    frame_us: int

    def advance(self, frame_us):
        elapsed = max(0, frame_us - self.frame_us) / 1_000_000
        self.frame_us = max(self.frame_us, frame_us)
        decay = math.exp(-4 * elapsed)
        distance = self.velocity * (1 - decay) / 4
        self.velocity *= decay
        return distance

    @property
    def active(self):
        return abs(self.velocity) >= 5
