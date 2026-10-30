# SPDX-License-Identifier: GPL-3.0-or-later
"""Analytic controls for the private Pulse microphone's existing tone thresholds."""

import importlib.util
import math
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("recording_signal", Path(__file__).parent / "recording/signal_power.py")
signal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signal)
RATE, FRAMES = 48000, 12000


def samples(*, jump=False, bleed=0.0, only_bleed=False, burst=False):
    return tuple(
        round(
            32768
            * (
                (
                    0
                    if only_bleed or burst and i >= FRAMES // 5
                    else 0.25 * math.sin(2 * math.pi * 660 * i / RATE + (math.pi if jump and i >= FRAMES // 2 else 0))
                )
                + bleed * math.sin(2 * math.pi * 880 * i / RATE)
            )
        )
        for i in range(FRAMES)
    )


def power(data, hz):
    return signal.energy(data, hz, RATE)["tone"] ** 2 / 4


def test_phase_jump_does_not_erase_a_continuous_tone():
    data = samples(jump=True)
    assert signal.energy(data, 660, RATE)["rms"] == pytest.approx(0.25 / math.sqrt(2), abs=2e-6)
    wanted, bleed = power(data, 660), power(data, 880)
    assert wanted > 0.001 and bleed < wanted * 0.01


def test_stable_tone_keeps_the_same_power_units():
    assert power(samples(), 660) == pytest.approx(0.25**2 / 4, abs=2e-6)


@pytest.mark.parametrize("kind", ["silence", "other-source", "brief-burst"])
def test_controls_still_fail_the_wanted_tone_threshold(kind):
    data = (
        bytes(FRAMES)
        if kind == "silence"
        else samples(only_bleed=True, bleed=0.25)
        if kind == "other-source"
        else samples(burst=True)
    )
    assert power(data, 660) < 0.001


def test_cross_source_bleed_still_fails_and_clean_signal_passes():
    clean, leaking = samples(bleed=0.01), samples(bleed=0.05)
    assert power(clean, 880) < power(clean, 660) * 0.01
    assert power(leaking, 880) > power(leaking, 660) * 0.01
