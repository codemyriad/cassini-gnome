# SPDX-License-Identifier: GPL-3.0-or-later
"""A window's painted render node, for the UI scripts' screenshots."""

import time

from gi.repository import GLib, Gtk


def apply_screenshot_scheme():
    """Apply light or dark colour scheme from CASSINI_SCREENSHOT_SCHEME."""
    import os

    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    if settings := Gtk.Settings.get_default():
        settings.props.gtk_cursor_blink = False
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK
        if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
        else Adw.ColorScheme.FORCE_LIGHT
    )


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
