# SPDX-License-Identifier: GPL-3.0-or-later
"""GlobalShortcuts portal session; approval and binding belong to the desktop.

https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html
"""

import logging
from uuid import uuid4

from gi.repository import Gio, GLib

from .strings import text

SERVICE = "org.freedesktop.portal.Desktop"
ROOT = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.GlobalShortcuts"


class GlobalShortcut:
    def __init__(self, activate, *, connection=None):
        self.activate = activate
        self.bus = connection
        self.session = None
        self.subscriptions = []
        self.requests = set()
        self.closed = False
        self.bound = False
        self.error = None
        if connection:
            self._create()
        else:
            Gio.bus_get(Gio.BusType.SESSION, None, self._connected)

    def _connected(self, _source, result):
        try:
            self.bus = Gio.bus_get_finish(result)
            if not self.closed:
                self._create()
        except GLib.Error as error:
            self._failed(error)

    def _failed(self, error):
        self.error = str(error)
        logging.getLogger(__name__).info("Global recording shortcut unavailable: %s", error)

    def _request(self, method, signature, values, completed):
        token = "cassini_" + uuid4().hex
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"{ROOT}/request/{sender}/{token}"
        options = {"handle_token": GLib.Variant("s", token)}
        if method == "CreateSession":
            options["session_handle_token"] = GLib.Variant("s", "cassini_" + uuid4().hex)
        self.requests.add(path)
        subscription = None

        def response(_bus, _sender, response_path, _interface, _signal, parameters):
            if response_path != path or self.closed:
                return
            self.bus.signal_unsubscribe(subscription)
            self.subscriptions.remove(subscription)
            self.requests.discard(path)
            code, result = parameters.unpack()
            if code == 0:
                completed(result)
            else:
                self._failed("Desktop declined the shortcut request")

        subscription = self.bus.signal_subscribe(
            SERVICE, "org.freedesktop.portal.Request", "Response", path, None, Gio.DBusSignalFlags.NONE, response
        )
        self.subscriptions.append(subscription)

        def called(bus, result):
            try:
                returned = bus.call_finish(result).unpack()[0]
                if returned != path:
                    self._failed("Portal returned an unexpected request path")
            except GLib.Error as error:
                if subscription in self.subscriptions:
                    self.bus.signal_unsubscribe(subscription)
                    self.subscriptions.remove(subscription)
                self.requests.discard(path)
                self._failed(error)

        self.bus.call(
            SERVICE,
            ROOT,
            INTERFACE,
            method,
            GLib.Variant(signature, (*values, options)),
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            called,
        )

    def _create(self):
        self.subscriptions.append(
            self.bus.signal_subscribe(
                SERVICE, INTERFACE, "Activated", ROOT, None, Gio.DBusSignalFlags.NONE, self._activated
            )
        )
        self._request("CreateSession", "(a{sv})", (), self._created)

    def _created(self, result):
        self.session = result["session_handle"]
        shortcuts = [
            (
                "record",
                {
                    "description": GLib.Variant("s", text("common.record")),
                    "preferred_trigger": GLib.Variant("s", "CTRL+ALT+r"),
                },
            )
        ]
        self._request("BindShortcuts", "(oa(sa{sv})sa{sv})", (self.session, shortcuts, ""), self._bound)

    def _bound(self, result):
        self.bound = any(identifier == "record" for identifier, _ in result.get("shortcuts", []))

    def _activated(self, _bus, _sender, _path, _interface, _signal, parameters):
        session, identifier, _timestamp, _options = parameters.unpack()
        if not self.closed and self.bound and session == self.session and identifier == "record":
            self.activate()

    def close(self):
        self.closed = True
        if self.bus:
            for subscription in self.subscriptions:
                self.bus.signal_unsubscribe(subscription)
            self.subscriptions.clear()
            for path in self.requests:
                self.bus.call(
                    SERVICE,
                    path,
                    "org.freedesktop.portal.Request",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                    None,
                )
            self.requests.clear()
            if self.session:
                self.bus.call(
                    SERVICE,
                    self.session,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                    None,
                )
