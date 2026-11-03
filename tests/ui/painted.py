# SPDX-License-Identifier: GPL-3.0-or-later
"""A window's painted render node, for the UI scripts' screenshots."""

import time

from gi.repository import GLib, Gtk


def painted_node(window, *, scale=1, timeout=10):
    """Snapshot the window once GTK has painted it, as spectrogram-live does.

    A WidgetPaintable shows the window's last render node, and any queue_draw
    since the last paint (a timer label, a level meter) drops that node, so a
    snapshot taken between the two is empty. Tick callbacks precede paint and
    cannot prove a node exists. Keep the paintable, ask for a frame while it
    is empty, and take it again after GTK paints; the timeout is a bound only.
    """
    paintable = Gtk.WidgetPaintable.new(window)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while True:
        snapshot = Gtk.Snapshot.new()
        if scale != 1:
            snapshot.scale(scale, scale)
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        node = snapshot.to_node()
        if node is not None:
            return node
        assert time.monotonic() < deadline, "Window snapshot stayed empty"
        window.queue_draw()
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)
