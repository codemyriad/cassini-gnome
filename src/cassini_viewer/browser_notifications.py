# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop notification with a Stop action for each automatic recording."""

import threading

_loop = None
_lock = threading.Lock()


def notify_recording(title, stop):
    from gi.repository import Gio, GLib

    global _loop
    with _lock:
        if _loop is None:
            _loop = GLib.MainLoop()
            threading.Thread(target=_loop.run, daemon=True, name="cassini-notifications").start()
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    result = bus.call_sync(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "Notify",
        GLib.Variant(
            "(susssasa{sv}i)",
            (
                "Cassini",
                0,
                "media-record-symbolic",
                "Recording browser meeting",
                GLib.markup_escape_text(title),
                ["stop", "Stop"],
                {"suppress-sound": GLib.Variant("b", True)},
                0,
            ),
        ),
        GLib.VariantType.new("(u)"),
        Gio.DBusCallFlags.NONE,
        1500,
        None,
    )
    identifier = result.unpack()[0]

    def activated(_connection, _sender, _path, _interface, _signal, parameters, *_args):
        notification, action = parameters.unpack()
        if notification == identifier and action == "stop":
            stop()

    subscription = bus.signal_subscribe(
        "org.freedesktop.Notifications",
        "org.freedesktop.Notifications",
        "ActionInvoked",
        "/org/freedesktop/Notifications",
        None,
        Gio.DBusSignalFlags.NONE,
        activated,
    )

    def close():
        bus.signal_unsubscribe(subscription)
        bus.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "CloseNotification",
            GLib.Variant("(u)", (identifier,)),
            None,
            Gio.DBusCallFlags.NONE,
            1500,
            None,
        )

    return close
