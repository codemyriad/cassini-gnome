# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-only PCM spectral energy; units and 100 ms windows match signal.mjs."""

import math


def energy(samples, hz, rate):
    # Pulse/browser clock corrections can change phase within the observation.
    # Sum magnitudes, as in signal.mjs, rather than cancelling opposite phases.
    # Weight by frames before squaring, so short bursts retain the old bound.
    window_frames = max(1, round(rate * 0.1))
    power, magnitude = 0.0, 0.0
    for first in range(0, len(samples), window_frames):
        real, imaginary = 0.0, 0.0
        for index in range(first, min(first + window_frames, len(samples))):
            value = samples[index] / 32768
            power += value * value
            real += value * math.cos(2 * math.pi * hz * index / rate)
            imaginary += value * math.sin(2 * math.pi * hz * index / rate)
        magnitude += math.hypot(real, imaginary)
    count = max(1, len(samples))
    return dict(rms=math.sqrt(power / count), tone=2 * magnitude / count)
