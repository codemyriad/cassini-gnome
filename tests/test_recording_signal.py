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


STEREO_RATE = 48000


def stereo(*, left_jump=0.0, right_jump=0.0, jump_at=1.5, right=880, left=440, bleed=0.0, right_silent=False):
    """2.5 s of interleaved S16 as native.py decodes it: 440 Hz left, 880 Hz right.

    A jump is the phase shift one Pulse skew correction (a 20 ms segment) makes.
    """
    frames = []
    for i in range(int(STEREO_RATE * 2.5)):
        t = i / STEREO_RATE
        after = t >= jump_at
        value_left = 0.2 * math.sin(2 * math.pi * left * t + (left_jump if after else 0)) + bleed * math.sin(
            2 * math.pi * 880 * t
        )
        value_right = 0 if right_silent else 0.2 * math.sin(2 * math.pi * right * t + (right_jump if after else 0))
        frames.extend((round(32767 * value_left), round(32767 * value_right)))
    return tuple(frames)


def native_bounds(amplitudes):
    """native.py's assertions, unchanged."""
    wanted = amplitudes["left440"] > 0.08 and amplitudes["right880"] > 0.08
    return wanted and amplitudes["left880"] < 0.01 and amplitudes["right440"] < 0.01


def coherent(samples, channel, hz):
    """The measurement native.py used before: one coefficient over frames 1-2 s."""
    data = samples[STEREO_RATE * 2 + channel : STEREO_RATE * 4 : 2]
    real = sum(v * math.cos(2 * math.pi * hz * n / STEREO_RATE) for n, v in enumerate(data))
    imaginary = sum(v * math.sin(2 * math.pi * hz * n / STEREO_RATE) for n, v in enumerate(data))
    return 2 * math.hypot(real, imaginary) / len(data) / 32768


@pytest.mark.parametrize(
    "jump",
    [
        dict(right_jump=math.pi),  # The hosted failure: right880 0.0019 coherent.
        dict(right_jump=math.radians(216)),  # Exactly one 20 ms segment at 880 Hz.
        dict(left_jump=math.pi),  # The microphone side (left440 0.0736 in run 40526670000).
        dict(right_jump=math.pi, jump_at=1.55),  # Inside a 100 ms window.
        dict(left_jump=math.pi, right_jump=math.radians(216)),
    ],
)
def test_native_stereo_proof_survives_a_skew_correction(jump):
    samples = stereo(**jump)
    amplitudes = signal.stereo_tones(samples, STEREO_RATE, STEREO_RATE, STEREO_RATE * 2, (440, 880))
    assert native_bounds(amplitudes), amplitudes
    assert amplitudes["leftRms"] == pytest.approx(0.2 / math.sqrt(2), rel=1e-3)


def test_the_coherent_measurement_reproduced_the_hosted_failure():
    samples = stereo(right_jump=math.pi)
    assert coherent(samples, 1, 880) < 0.01  # The full-level tone cancelled itself.
    assert coherent(samples, 0, 440) > 0.08


@pytest.mark.parametrize(
    "fault",
    [dict(right_silent=True), dict(left=880, right=440), dict(bleed=0.05)],
    ids=["right-silent", "channels-swapped", "bleed-into-left"],
)
def test_native_stereo_controls_still_fail(fault):
    samples = stereo(**fault)
    amplitudes = signal.stereo_tones(samples, STEREO_RATE, STEREO_RATE, STEREO_RATE * 2, (440, 880))
    assert not native_bounds(amplitudes), amplitudes
