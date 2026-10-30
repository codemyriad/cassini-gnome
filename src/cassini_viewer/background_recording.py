# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit background consent; no autostart or automatic permission prompt.

https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Background.html
"""

import contextlib
import logging
import threading
from uuid import uuid4

from gi.repository import Gio, GLib

from .strings import CATALOG, text

LOG = logging.getLogger(__name__)
SERVICE = "org.freedesktop.portal.Desktop"
ROOT = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.Background"


class BackgroundRecording:
    def __init__(self, application_id=None, *, native=False):
        self.application_id = application_id
        self.native = native
        self.bus = None
        self.callback = None
        self.path = None
        self.subscription = self.timer = 0
        self.closed = False
        self.status_supported = True
        self.last_status = None
        self.status_confirmed = False
        self.generation = 0
        self.granted = False
        self.cancellable = None

    def request(self, callback):
        if self.granted and not self.closed:
            callback(True)
            return
        if self.closed or self.callback is not None:
            callback(False)
            return
        self.callback = callback
        self.generation += 1
        generation = self.generation
        self.cancellable = Gio.Cancellable()
        cancellable = self.cancellable
        self.timer = GLib.timeout_add_seconds(120, self._expired)

        # A private connection lets native apps register before *any* portal
        # method, independently of GTK's Settings/FileChooser connection.
        # Address discovery and registration are bounded worker IO.
        def connect():
            bus, error = self.bus, None
            try:
                if bus is None:
                    address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, cancellable)
                    bus = Gio.DBusConnection.new_for_address_sync(
                        address,
                        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                        None,
                        cancellable,
                    )
                    if self.native and self.application_id:
                        bus.call_sync(
                            SERVICE,
                            ROOT,
                            "org.freedesktop.host.portal.Registry",
                            "Register",
                            GLib.Variant("(sa{sv})", (self.application_id, {})),
                            None,
                            Gio.DBusCallFlags.NONE,
                            5000,
                            cancellable,
                        )
            except GLib.Error as failure:
                error = failure
            GLib.idle_add(self._connected, bus, error, generation)

        threading.Thread(target=connect, name="cassini-background-portal", daemon=True).start()

    def _connected(self, bus, error, generation):
        try:
            if self.closed or self.callback is None or generation != self.generation:
                if bus is not None and bus is not self.bus:
                    bus.close(None, None, None)
                return
            if error:
                if bus is not None:
                    bus.close(None, None, None)
                self._failed(error)
                return False
            self.bus = bus
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
        return False

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
        if self.cancellable:
            self.cancellable.cancel()
        self._cancel_request()
        self._complete(False)

    def _complete(self, allowed):
        self.granted = bool(allowed) and not self.closed
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

    def status(self, message, callback=None):
        # SetStatus is Background v2. Version 1 still supports permission and
        # the persistent notification; do not repeatedly call a missing method.
        message = " ".join(message.split())[:96]
        if self.closed or self.bus is None or not self.status_supported or message == self.last_status:
            if callback:
                callback(not self.closed and self.status_confirmed and self.status_supported)
            return
        self.last_status = message
        self.status_confirmed = False

        def done(bus, result):
            success = True
            try:
                bus.call_finish(result)
            except GLib.Error as error:
                LOG.info("Background status unavailable: %s", error)
                self.status_supported = False
                success = False
            if callback:
                callback(success and not self.closed)
            if message == self.last_status:
                self.status_confirmed = success and not self.closed

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
        if self.cancellable:
            self.cancellable.cancel()
        self._cancel_request()
        self._complete(False)
        if self.bus:
            # GDBus closes before writing queued messages; flush first so the
            # cleared status and the pending request's Close reach the portal.
            bus, self.bus = self.bus, None

            def flushed(connection, result):
                with contextlib.suppress(GLib.Error):
                    connection.flush_finish(result)
                connection.close(None, None, None)

            bus.flush(None, flushed)
