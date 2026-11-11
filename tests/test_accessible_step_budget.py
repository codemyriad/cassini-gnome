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


class _FakeTimeoutSource:
    def __init__(self, delay_ms: int):
        self.delay_ms = delay_ms
        self.callback = None
        self.attached_to = None
        self.destroyed = False

    def set_callback(self, callback):
        self.callback = callback

    def attach(self, context):
        assert not self.destroyed
        self.attached_to = context
        context.sources.append(self)

    def destroy(self):
        self.destroyed = True
        if self.attached_to is not None and self in self.attached_to.sources:
            self.attached_to.sources.remove(self)


class _FakeMainContext:
    def __init__(self, on_iterate):
        self.sources: list[_FakeTimeoutSource] = []
        self._on_iterate = on_iterate

    def iteration(self, may_block: bool):
        assert may_block is True
        assert self.sources, "MainContext.iteration(True) entered with no timeout guard attached"
        self._on_iterate(self)


def test_early_guard_expiry_rearms_remaining_budget_and_fails_at_deadline():
    now = [0.0]
    created_delays = []

    def make_source(delay_ms: int):
        created_delays.append(delay_ms)
        return _FakeTimeoutSource(delay_ms)

    def on_iterate(ctx: _FakeMainContext):
        (source,) = ctx.sources
        if len(created_delays) == 1:
            # Simulate GLib waking a 55 s timeout source early at 54.78 s with no event progress.
            now[0] = 54.78
        else:
            now[0] += source.delay_ms / 1000.0
        keep = source.callback()
        if not keep and source in ctx.sources:
            ctx.sources.remove(source)

    context = _FakeMainContext(on_iterate)
    with pytest.raises(AssertionError, match=r"55s step budget; events\[-1\]=ready"):
        progress_wait.until_context_progress(
            lambda: False,
            context=context,
            timeout_source_ms=make_source,
            source_remove=False,
            progress=lambda: 1,
            diagnostics=lambda: "events[-1]=ready",
            clock=lambda: now[0],
        )

    assert created_delays == [55000, 220]
    assert now[0] == pytest.approx(55.0)
    assert context.sources == []


def test_progress_replaces_active_guard_and_destroys_guard_when_satisfied():
    now = [0.0]
    events = ["ready"]
    created: list[_FakeTimeoutSource] = []

    def make_source(delay_ms: int):
        source = _FakeTimeoutSource(delay_ms)
        created.append(source)
        return source

    def on_iterate(ctx: _FakeMainContext):
        assert len(ctx.sources) == 1
        if len(events) == 1:
            now[0] = 10.0
            events.append("speaker")
        else:
            now[0] = 12.0
            events.append("search-play")

    context = _FakeMainContext(on_iterate)
    progress_wait.until_context_progress(
        lambda: "search-play" in events,
        context=context,
        timeout_source_ms=make_source,
        source_remove=False,
        progress=lambda: len(events),
        clock=lambda: now[0],
    )

    assert [s.delay_ms for s in created] == [55000, 55000]
    assert all(s.destroyed for s in created)
    assert context.sources == []
