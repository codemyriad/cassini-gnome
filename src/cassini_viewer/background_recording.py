# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit background consent; no autostart or automatic permission prompt.

https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Background.html
"""

import logging
from uuid import uuid4

from gi.repository import Gio, GLib

from .strings import CATALOG, text

LOG = logging.getLogger(__name__)
SERVICE = "org.freedesktop.portal.Desktop"
ROOT = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.Background"


class BackgroundRecording:
    def __init__(self):
        self.bus = None
        self.callback = None
        self.path = None
        self.subscription = self.timer = 0
        self.closed = False
        self.status_supported = True
        self.last_status = None
        self.generation = 0

    def request(self, callback):
        if self.closed or self.callback is not None:
            callback(False)
            return
        self.callback = callback
        self.generation += 1
        generation = self.generation
        Gio.bus_get(Gio.BusType.SESSION, None, lambda source, result: self._connected(source, result, generation))

    def _connected(self, _source, result, generation):
        try:
            self.bus = Gio.bus_get_finish(result)
            if self.closed or self.callback is None or generation != self.generation:
                return
            token = "cassini_" + uuid4().hex
            sender = self.bus.get_unique_name()[1:].replace(".", "_")
            self.path = f"{ROOT}/request/{sender}/{token}"
            # Subscribe before calling: the portal can respond immediately.
            self.subscription = self.bus.signal_subscribe(
                SERVICE,
                "org.freedesktop.portal.Request",
                "Response",
                self.path,
                None,
                Gio.DBusSignalFlags.NONE,
                self._response,
            )
            self.timer = GLib.timeout_add_seconds(120, self._expired)
            options = {
                "handle_token": GLib.Variant("s", token),
                "autostart": GLib.Variant("b", False),
                "reason": GLib.Variant(
                    "s",
                    text("recording.background.reason")
                    if "recording.background.reason" in CATALOG
                    else text("recording.background.notification"),
                ),
            }
            self.bus.call(
                SERVICE,
                ROOT,
                INTERFACE,
                "RequestBackground",
                GLib.Variant("(sa{sv})", ("", options)),
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
                lambda bus, result: self._called(bus, result, generation),
            )
        except GLib.Error as error:
            self._failed(error)

    def _called(self, bus, result, generation):
        try:
            returned = bus.call_finish(result).unpack()[0]
            if generation != self.generation:
                return
            if self.path is not None and returned != self.path:
                self._failed("Unexpected background request path")
        except GLib.Error as error:
            if generation == self.generation:
                self._failed(error)

    def _response(self, _bus, _sender, path, _interface, _signal, parameters):
        if self.callback is not None and path == self.path:
            code, result = parameters.unpack()
            self._complete(code == 0 and result.get("background") is True)

    def _expired(self):
        self.timer = 0
        self._cancel_request()
        self._failed("Background request expired; keeping the window open")
        return GLib.SOURCE_REMOVE

    def _failed(self, error):
        LOG.info("Background recording permission unavailable: %s", error)
        self._cancel_request()
        self._complete(False)

    def _complete(self, allowed):
        callback, self.callback = self.callback, None
        if self.subscription:
            self.bus.signal_unsubscribe(self.subscription)
            self.subscription = 0
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.path = None
        if callback:
            callback(bool(allowed) and not self.closed)

    def status(self, message):
        # SetStatus is Background v2. Version 1 still supports permission and
        # the persistent notification; do not repeatedly call a missing method.
        message = " ".join(message.split())[:96]
        if self.closed or self.bus is None or not self.status_supported or message == self.last_status:
            return
        self.last_status = message

        def done(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error:
                self.status_supported = False

        self.bus.call(
            SERVICE,
            ROOT,
            INTERFACE,
            "SetStatus",
            GLib.Variant("(a{sv})", ({"message": GLib.Variant("s", message)},)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            done,
        )

    def _cancel_request(self):
        if self.bus and self.path:
            self.bus.call(
                SERVICE,
                self.path,
                "org.freedesktop.portal.Request",
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
                None,
            )

    def close(self):
        if self.closed:
            return
        self.status("")
        self.closed = True
        self._cancel_request()
        self._complete(False)
