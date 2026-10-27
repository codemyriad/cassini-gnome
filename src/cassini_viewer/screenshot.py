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
gi.require_version("Gst", "1.0")
from gi.repository import Adw, GLib, Gst, Gtk  # noqa: E402


def capture(application_id, version, profile):
    if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("Use build-aux/ux-screenshot.sh in a private display")
    from .main import CassiniApplication

    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Gtk.Settings.get_default().props.gtk_font_name = "Cantarell 11"
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK
        if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
        else Adw.ColorScheme.FORCE_LIGHT
    )
    app = CassiniApplication(application_id, version, profile)
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    paintable = Gtk.WidgetPaintable.new(window)
    window.queue_draw()
    frames = [0]
    settled_frame = None
    window.add_tick_callback(lambda *_args: (frames.__setitem__(0, frames[0] + 1), True)[1])
    opened_meeting = False
    opened_record_first = False
    opened_recording = False
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if (
            os.environ.get("CASSINI_SCREENSHOT_BOARD") == "record-first"
            and not window._pending
            and not opened_record_first
        ):
            app.on_record()
            opened_record_first = True
            settled_frame = None
        if os.environ.get("CASSINI_SCREENSHOT_BOARD") == "recording" and not window._pending and not opened_recording:
            from .recording_comfort import notification_payload
            from .recording_details import recording_observation

            meeting = next(m for m in window.snapshot.meetings if m.lifecycle == "recording")
            observed = window.monitor._executor.submit(recording_observation, meeting).result(timeout=5)
            # Derived fixture contains a microphone silent for the entire 12:48;
            # render the detector's >=20-second state, not a live host device.
            app.comfort.states[meeting.key] = dict(observed, silent=observed["levels"][0] == 0, line=None)
            window.open_meeting(meeting)
            window._update_live()
            target.with_name("notification.json").write_text(
                json.dumps(notification_payload(meeting, app.comfort.elapsed(meeting)), indent=2) + "\n"
            )
            opened_recording = True
            settled_frame = None
        if os.environ.get("CASSINI_SCREENSHOT_BOARD") == "meeting" and not window._pending and not opened_meeting:
            from .folder_player import FolderPlayer

            window.player.shutdown()
            sink = Gst.ElementFactory.make("fakesink", None)
            sink.set_property("sync", True)
            window.player = FolderPlayer(audio_sink=sink)
            window._connect_player()
            meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
            window.open_meeting(meeting)
            window.player.seek(192000)
            window.player.play()
            opened_meeting = True
            settled_frame = None
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
    if (texture.get_width(), texture.get_height()) != (1120, 720):
        raise RuntimeError(f"Expected 1120x720, got {texture.get_width()}x{texture.get_height()}")
    if not texture.save_to_png(str(target)):
        raise RuntimeError("GDK could not write screenshot PNG")
    target.with_suffix(".json").write_text(
        json.dumps(
            dict(
                surface=os.environ.get("CASSINI_SCREENSHOT_BOARD", "current-main"),
                defaultSize=[1130, 730],
                allocation=[window.get_width(), window.get_height()],
                width=texture.get_width(),
                height=texture.get_height(),
                renderer=os.environ.get("GSK_RENDERER"),
                tz=os.environ.get("TZ"),
                lang=os.environ.get("LANG"),
                font=Gtk.Settings.get_default().props.gtk_font_name,
                accent=Adw.StyleManager.get_default().get_accent_color().value_nick,
                light=not Adw.StyleManager.get_default().get_dark(),
                limitations=[
                    "GNotification button appearance is controlled by GNOME Shell.",
                ],
            ),
            indent=2,
        )
        + "\n"
    )
    window.close()
    print(f"Saved real GTK current-main window to {target}")
    return 0
