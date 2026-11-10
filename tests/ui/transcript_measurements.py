# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame-clock/CPU helpers adapted from PR #310's transcript-scroll harness."""

import time

from gi.repository import GLib, Gtk


def dispatch_until(check):
    context = GLib.MainContext.default()
    expired = []
    timeout = GLib.timeout_add_seconds(15, lambda: (expired.append(True), GLib.SOURCE_REMOVE)[1])
    try:
        while not check():
            assert not expired, "Timed out waiting for GTK state/input"
            context.iteration(True)
    finally:
        if not expired:
            GLib.source_remove(timeout)


def paint(window):
    loop = GLib.MainLoop()
    clock = window.get_frame_clock()
    handler = clock.connect("after-paint", lambda *_: loop.quit())
    window.add_tick_callback(lambda *_: GLib.SOURCE_REMOVE)
    try:
        loop.run()
    finally:
        clock.disconnect(handler)


class FrameCPU:
    def __init__(self, window):
        self.clock = window.get_frame_clock()
        self.samples = []
        self.started = 0
        self.previous = time.thread_time_ns()
        self.between_paints = []
        self.before = self.clock.connect("before-paint", self._before)
        self.after = self.clock.connect("after-paint", self._after)

    def _before(self, *_args):
        self.started = time.thread_time_ns()

    def _after(self, *_args):
        now = time.thread_time_ns()
        self.samples.append((now - self.started) / 1e6)
        self.between_paints.append((now - self.previous) / 1e6)
        self.previous = now

    def close(self):
        self.clock.disconnect(self.before)
        self.clock.disconnect(self.after)


def layout_idle(window):
    """GTK 4.22 validates at priority 125 until the entire buffer is valid.

    A lower-priority watermark runs after those idles finish. Observe the next
    painted frame so the frame clock includes validation, without time sleeps.
    """
    ready = []
    GLib.idle_add(lambda: (ready.append(True), GLib.SOURCE_REMOVE)[1], priority=Gtk.TEXT_VIEW_PRIORITY_VALIDATE + 1)
    dispatch_until(lambda: ready)
    paint(window)
