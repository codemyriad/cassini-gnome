# SPDX-License-Identifier: GPL-3.0-or-later
"""A finite budget for each observed step of a UI interaction sequence."""

import math
import time


def until_progress(
    check,
    *,
    progress,
    dispatch,
    timeout=55,
    diagnostics=lambda: "",
    clock=time.monotonic,
    pass_deadline=False,
):
    observed = progress()
    deadline = clock() + timeout
    while not check():
        if pass_deadline:
            dispatch(deadline)
        else:
            dispatch()
        now = clock()
        if now >= deadline:
            raise AssertionError(f"AT-SPI activation exceeded its {timeout}s step budget; {diagnostics()}")
        current = progress()
        if current != observed:
            observed = current
            deadline = now + timeout


def until_context_progress(
    check,
    *,
    context,
    timeout_source_ms,
    progress,
    source_remove=False,
    timeout=55,
    diagnostics=lambda: "",
    clock=time.monotonic,
):
    """Dispatch `context` against a shared step deadline, rearming any early timeout wake."""
    guard = None
    armed_deadline = None

    def clear_guard():
        nonlocal guard, armed_deadline
        if guard is not None:
            guard.destroy()
            guard = None
            armed_deadline = None

    def on_timeout(*_args):
        nonlocal guard, armed_deadline
        guard = None
        armed_deadline = None
        return source_remove

    def dispatch_until_deadline(deadline):
        nonlocal guard, armed_deadline
        remaining = deadline - clock()
        if remaining <= 0:
            clear_guard()
            return
        if guard is not None and armed_deadline != deadline:
            clear_guard()
        if guard is None:
            guard = timeout_source_ms(max(1, math.ceil(remaining * 1000)))
            guard.set_callback(on_timeout)
            guard.attach(context)
            armed_deadline = deadline
        context.iteration(True)

    try:
        until_progress(
            check,
            progress=progress,
            dispatch=dispatch_until_deadline,
            timeout=timeout,
            diagnostics=diagnostics,
            clock=clock,
            pass_deadline=True,
        )
    finally:
        clear_guard()
