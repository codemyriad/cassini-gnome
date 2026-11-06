# SPDX-License-Identifier: GPL-3.0-or-later
"""A finite budget for each observed step of a UI interaction sequence."""

import time


def until_progress(check, *, progress, dispatch, timeout=55, diagnostics=lambda: "", clock=time.monotonic):
    observed = progress()
    deadline = clock() + timeout
    while not check():
        dispatch()
        now = clock()
        if now >= deadline:
            raise AssertionError(f"AT-SPI activation exceeded its {timeout}s step budget; {diagnostics()}")
        current = progress()
        if current != observed:
            observed = current
            deadline = now + timeout
