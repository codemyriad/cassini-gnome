# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Kinetic integration uses injected frame stamps, never elapsed-time budgets."""

import math

import pytest

from cassini_talk.kinetic_scroll import KineticMotion


@pytest.mark.parametrize("velocity", [-12000, 12000])
def test_motion_is_directional_and_decays_to_a_finite_distance(velocity):
    motion = KineticMotion(velocity, 0)
    deltas = [motion.advance(frame * 16000) for frame in range(1, 200)]
    assert all(math.copysign(1, delta) == math.copysign(1, velocity) for delta in deltas)
    assert all(abs(b) < abs(a) for a, b in zip(deltas, deltas[1:], strict=False))
    assert abs(sum(deltas)) < abs(velocity / 4)
    assert not motion.active


def test_frame_partition_does_not_change_the_gesture_distance():
    many = KineticMotion(9000, 1000000)
    distance = sum(many.advance(t) for t in range(1010000, 2000001, 10000))
    one = KineticMotion(9000, 1000000)
    assert distance == pytest.approx(one.advance(2000000))
    assert many.velocity == pytest.approx(one.velocity)


@pytest.mark.parametrize("correction", [-50000, 50000])
def test_layout_coordinate_rebase_does_not_change_the_next_frame_delta(correction):
    motion = KineticMotion(10000, 0)
    value = 1000 + motion.advance(16000)
    value += correction  # A row-height correction can move the pixel origin.
    corrected_origin = value
    value += motion.advance(32000)
    reference = KineticMotion(10000, 0)
    reference.advance(16000)
    assert value - corrected_origin == pytest.approx(reference.advance(32000))
    assert value > corrected_origin


def test_duplicate_or_old_frame_timestamp_does_not_advance_or_reverse_motion():
    motion = KineticMotion(3000, 500000)
    assert motion.advance(500000) == 0
    assert motion.advance(499000) == 0
    assert motion.frame_us == 500000
    assert motion.velocity == 3000
    assert motion.advance(516000) > 0


def test_new_opposite_gesture_has_no_residual_forward_velocity():
    old = KineticMotion(5000, 0)
    old.advance(16000)
    replacement = KineticMotion(-2000, 16000)
    assert replacement.advance(32000) < 0


def test_stalled_frame_still_has_bounded_analytical_work_and_travel():
    motion = KineticMotion(5000, 0)
    assert motion.advance(60000000) == pytest.approx(1250)
    assert not motion.active
