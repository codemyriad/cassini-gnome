# SPDX-License-Identifier: GPL-3.0-or-later
"""One clock base for a capture's session timeline (SPEC "Page trust and browser clocks", D61).

A capture's session origin, timelineOriginMs, is a wall-clock time (Unix ms). It is
mapped onto the system monotonic clock (time.monotonic_ns: CLOCK_MONOTONIC, the clock
GStreamer's SystemClock and the browser protocol driver read) once, by one wall reading
between two monotonic readings, and the wall clock is never read again for that capture.
A wall clock that steps (settimeofday, chrony makestep, a VM resync) or drifts during
the capture then moves no stamp, as the extension and the protocol driver
(scenarios/session-clock.mjs) also measure on the monotonic clock.

What one mapping per party cannot fix: each party maps the same origin with its own wall
reading. The extension maps it per document, at each AudioContext's calibration
(capture.js measure(): Date.now(), performance.now(), Date.now()); GNOME maps it once at
BrowserCapture.start(); the protocol driver maps it when it learns the origin
(sessionClock()). A wall step S that lands before all of these readings shifts every
party equally, so nothing is misaligned. A step that lands between the extension's
calibration and GNOME's start (readings typically a few hundred ms apart) leaves the
browser's PCM S away from GNOME's events and native PCM for the whole capture; the
driver has the same exposure between its mapping and the others'. And after a mid-capture step, a browser
resume calibrates a new document on the stepped wall clock, so browser spans after it sit
S away from GNOME's kept mapping. That follows from SPEC's per-document mapping, not from
GNOME: re-mapping GNOME at the resume would instead put GNOME's own stamps before and
after it on two bases.
"""

import time


class SessionClock:
    """A capture's timelineOriginMs mapped onto CLOCK_MONOTONIC, taken once."""

    __slots__ = ("origin_ms", "origin_monotonic_ns")

    def __init__(self, origin_ms=None):
        """Map origin_ms; with None, the origin is this wall reading itself (an origin
        GNOME picks), so nothing can step between the origin and its mapping."""
        before = time.monotonic_ns()
        wall = time.time_ns()
        after = time.monotonic_ns()
        self.origin_ms = wall // 1_000_000 if origin_ms is None else origin_ms
        # The monotonic reading at which the wall clock read origin_ms.
        self.origin_monotonic_ns = (before + after) // 2 - (wall - self.origin_ms * 1_000_000)

    def at(self, monotonic_ns):
        """The session time (ms, may be negative) of a CLOCK_MONOTONIC reading in ns."""
        return (monotonic_ns - self.origin_monotonic_ns) / 1_000_000

    def now(self):
        """The current session time in ms; reads only the monotonic clock."""
        return self.at(time.monotonic_ns())
