# SPDX-License-Identifier: GPL-3.0-or-later
"""Hidden screenshot spike: snapshot the real current window, never the desktop."""

import json
import os
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


def capture(application_id, version, profile):
    if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("Use build-aux/ux-screenshot.sh in a private display")
    from .main import CassiniApplication

    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Gtk.Settings.get_default().props.gtk_font_name = "Cantarell 11"
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    app = CassiniApplication(application_id, version, profile)
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1120, 720)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    paintable = Gtk.WidgetPaintable.new(window)
    window.queue_draw()
    frames = [0]
    settled_frame = None
    window.add_tick_callback(lambda *_args: (frames.__setitem__(0, frames[0] + 1), True)[1])
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if getattr(window, "_pending", False):
            settled_frame = None
        elif settled_frame is None:
            settled_frame = frames[0]
        if window.get_width() > 0 and settled_frame is not None and frames[0] >= settled_frame + 2:
            snapshot = Gtk.Snapshot.new()
            paintable.snapshot(snapshot, window.get_width(), window.get_height())
            node = snapshot.to_node()
            if node is not None:
                break
        time.sleep(0.01)
    else:
        raise RuntimeError(
            f"Current window did not settle: size={window.get_width()}x{window.get_height()}, frames={frames[0]}, pending={getattr(window, '_pending', None)}"
        )
    viewport = node.get_bounds()
    texture = window.get_renderer().render_texture(node, viewport)
    if not texture.save_to_png(str(target)):
        raise RuntimeError("GDK could not write screenshot PNG")
    target.with_suffix(".json").write_text(
        json.dumps(
            dict(
                surface="current-main",
                width=texture.get_width(),
                height=texture.get_height(),
                renderer=os.environ.get("GSK_RENDERER"),
                tz=os.environ.get("TZ"),
                lang=os.environ.get("LANG"),
                font=Gtk.Settings.get_default().props.gtk_font_name,
                accent=Adw.StyleManager.get_default().get_accent_color().value_nick,
                light=not Adw.StyleManager.get_default().get_dark(),
                limitations=[
                    "Current BrowserMeetingsWindow; no Library shell or design-board fidelity is claimed.",
                    "Fixture and fixed-clock rendering are deferred to the coordinator screenshot contract.",
                ],
            ),
            indent=2,
        )
        + "\n"
    )
    window.close()
    print(f"Saved real GTK current-main window to {target}")
    return 0
