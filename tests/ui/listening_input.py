# SPDX-License-Identifier: GPL-3.0-or-later
"""Real keys in an owned Xvfb, preserving an active GTK popup grab."""

import ctypes
import subprocess

from gi.repository import Gdk, GdkX11, Gtk


def key(window, sequence, until):
    xid = str(GdkX11.X11Surface.get_xid(window.get_surface()))
    until(
        window,
        lambda: (
            window.get_focus() is not None and window.get_focus().get_mapped() and window.get_focus().get_width() > 0
        ),
    )
    # Xvfb has no window manager to restore native focus after a popup
    # unmaps. Preserve a live popup's grab; otherwise focus the real root.
    widget = window.get_focus()
    in_popover = False
    while widget:
        if isinstance(widget, Gtk.Popover) and widget.get_mapped():
            in_popover = True
            break
        widget = widget.get_parent()
    if not in_popover:
        window.get_display().sync()
        subprocess.run(["xdotool", "windowfocus", "--sync", xid], check=True, timeout=5)
    x11, xtst = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(None)
    assert display
    try:
        # Clear native modifiers without xdotool's restore pass, which can
        # turn a later plain F9 into Alt+F9 after a popup's keyboard grab.
        for symbol in (
            Gdk.KEY_Alt_L,
            Gdk.KEY_Alt_R,
            Gdk.KEY_Shift_L,
            Gdk.KEY_Shift_R,
            Gdk.KEY_Control_L,
            Gdk.KEY_Control_R,
            Gdk.KEY_Super_L,
            Gdk.KEY_Super_R,
        ):
            xtst.XTestFakeKeyEvent(display, x11.XKeysymToKeycode(display, symbol), False, 0)
        symbols = {
            "alt": Gdk.KEY_Alt_L,
            "shift": Gdk.KEY_Shift_L,
            "Down": Gdk.KEY_Down,
            "Up": Gdk.KEY_Up,
            "Escape": Gdk.KEY_Escape,
            "F9": Gdk.KEY_F9,
            "Return": Gdk.KEY_Return,
            "Tab": Gdk.KEY_Tab,
            "KP_Enter": Gdk.KEY_KP_Enter,
            "Page_Down": Gdk.KEY_Page_Down,
            "KP_Page_Down": Gdk.KEY_KP_Page_Down,
        }
        codes = [x11.XKeysymToKeycode(display, symbols[part]) for part in sequence.split("+")]
        assert all(codes)
        for code in codes:
            xtst.XTestFakeKeyEvent(display, code, True, 0)
        for code in reversed(codes):
            xtst.XTestFakeKeyEvent(display, code, False, 0)
        x11.XSync(display, False)
    finally:
        x11.XCloseDisplay(display)
