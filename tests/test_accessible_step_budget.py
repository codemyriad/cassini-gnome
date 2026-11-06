# SPDX-License-Identifier: GPL-3.0-or-later
"""A long AT-SPI sequence may pass; a single stalled step must still fail."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("progress_wait", Path(__file__).parent / "ui/progress_wait.py")
progress_wait = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress_wait)


def test_seven_bounded_steps_can_exceed_the_old_total_deadline_without_sleeping():
    now, events = [0], []

    def dispatch():
        now[0] += 1
        if now[0] % 9 == 0:
            events.append(f"step-{len(events) + 1}")

    progress_wait.until_progress(
        lambda: len(events) == 7,
        progress=lambda: len(events),
        dispatch=dispatch,
        clock=lambda: now[0],
    )
    assert now[0] == 63 > 55


@pytest.mark.parametrize("late_progress", [False, True])
def test_one_step_over_the_same_budget_fails_even_if_it_eventually_advances(late_progress):
    now, events = [0], ["speaker"]

    def dispatch():
        now[0] += 56
        if late_progress:
            events.append("too-late")

    with pytest.raises(AssertionError, match=r"55s step budget; events\[-1\]=speaker; playing=False; position=0"):
        progress_wait.until_progress(
            lambda: False,
            progress=lambda: len(events),
            dispatch=dispatch,
            diagnostics=lambda: "events[-1]=speaker; playing=False; position=0",
            clock=lambda: now[0],
        )


def test_dispatch_without_progress_does_not_renew_the_budget():
    now = [0]

    def dispatch():
        now[0] += 10

    with pytest.raises(AssertionError, match="55s step budget"):
        progress_wait.until_progress(lambda: False, progress=lambda: 0, dispatch=dispatch, clock=lambda: now[0])
    assert now[0] == 60
