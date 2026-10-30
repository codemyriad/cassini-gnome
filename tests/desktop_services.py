# SPDX-License-Identifier: GPL-3.0-or-later
"""Private-bus doubles for the actual portal and GNotification wire protocols."""

from gi.repository import Gio, GLib

PORTAL_XML = """<node><interface name="org.freedesktop.portal.GlobalShortcuts">
<method name="CreateSession"><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
<method name="BindShortcuts"><arg type="o" direction="in"/><arg type="a(sa{sv})" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
<method name="ListShortcuts"><arg type="o" direction="in"/><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
<method name="ConfigureShortcuts"><arg type="o" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/></method>
<signal name="ShortcutsChanged"><arg type="o"/><arg type="a(sa{sv})"/></signal>
<signal name="Activated"><arg type="o"/><arg type="s"/><arg type="t"/><arg type="a{sv}"/></signal>
</interface></node>"""
BACKGROUND_XML = """<node><interface name="org.freedesktop.portal.Background">
<method name="RequestBackground"><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
<method name="SetStatus"><arg type="a{sv}" direction="in"/></method>
</interface></node>"""
REGISTRY_XML = """<node><interface name="org.freedesktop.host.portal.Registry">
<method name="Register"><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/></method>
</interface></node>"""
REQUEST_XML = """<node><interface name="org.freedesktop.portal.Request"><method name="Close"/></interface></node>"""
NOTIFY_XML = """<node><interface name="org.gtk.Notifications">
<method name="AddNotification"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/></method>
<method name="RemoveNotification"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>"""
SESSION_XML = """<node><interface name="org.freedesktop.portal.Session"><method name="Close"/></interface></node>"""


class DesktopServices:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.notifications = {}
        self.history = []
        self.calls = []
        self.session = "/org/freedesktop/portal/desktop/session/cassini_test"
        self.denied = False
        self.background_denied = False
        self.background_defer = False
        self.background_pending = []
        self.background_error = False
        self.background_closed = 0
        self.shortcut_description = "Ctrl+Alt+R"
        self.shortcuts_bound = False
        self.closed = False
        self.owners = [
            Gio.bus_own_name_on_connection(self.bus, name, Gio.BusNameOwnerFlags.NONE, None, None)
            for name in ("org.freedesktop.portal.Desktop", "org.gtk.Notifications")
        ]
        self.objects = [
            self.bus.register_object(
                "/org/freedesktop/portal/desktop", Gio.DBusNodeInfo.new_for_xml(PORTAL_XML).interfaces[0], self._portal
            ),
            self.bus.register_object(
                "/org/freedesktop/portal/desktop",
                Gio.DBusNodeInfo.new_for_xml(BACKGROUND_XML).interfaces[0],
                self._background,
            ),
            self.bus.register_object(
                "/org/freedesktop/portal/desktop",
                Gio.DBusNodeInfo.new_for_xml(REGISTRY_XML).interfaces[0],
                self._register,
            ),
            self.bus.register_object(
                "/org/gtk/Notifications", Gio.DBusNodeInfo.new_for_xml(NOTIFY_XML).interfaces[0], self._notification
            ),
            self.bus.register_object(
                self.session, Gio.DBusNodeInfo.new_for_xml(SESSION_XML).interfaces[0], self._close
            ),
        ]

    def _portal(self, _bus, sender, _path, _interface, method, parameters, invocation):
        values = parameters.unpack()
        self.calls.append(dict(method=method, values=values))
        if method == "ConfigureShortcuts":
            invocation.return_value(None)
            self.bus.emit_signal(
                sender,
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.GlobalShortcuts",
                "ShortcutsChanged",
                GLib.Variant(
                    "(oa(sa{sv}))",
                    (self.session, [("record", {"trigger_description": GLib.Variant("s", self.shortcut_description)})]),
                ),
            )
            return
        token = values[-1]["handle_token"]
        path = f"/org/freedesktop/portal/desktop/request/{sender[1:].replace('.', '_')}/{token}"
        if method == "CreateSession":
            result = {"session_handle": GLib.Variant("s", self.session)}
        else:
            rows = (
                [("record", {"trigger_description": GLib.Variant("s", self.shortcut_description)})]
                if method == "BindShortcuts" or self.shortcuts_bound
                else []
            )
            if method == "BindShortcuts":
                self.shortcuts_bound = True
            result = {"shortcuts": GLib.Variant("a(sa{sv})", rows)}
        invocation.return_value(GLib.Variant("(o)", (path,)))
        # Deliberately emit immediately, before the method callback is handled.
        self.bus.emit_signal(
            sender,
            path,
            "org.freedesktop.portal.Request",
            "Response",
            GLib.Variant("(ua{sv})", (1 if self.denied else 0, result)),
        )

    def _register(self, _bus, sender, _path, _interface, method, parameters, invocation):
        self.calls.append(dict(method=method, sender=sender, values=parameters.unpack()))
        invocation.return_value(None)

    def _background(self, _bus, sender, _path, _interface, method, parameters, invocation):
        values = parameters.unpack()
        self.calls.append(dict(method=method, sender=sender, values=values))
        if self.background_error:
            invocation.return_dbus_error("org.freedesktop.portal.Error.Failed", "Synthetic unavailable portal")
        elif method == "SetStatus":
            invocation.return_value(None)
        else:
            token = values[-1]["handle_token"]
            path = f"/org/freedesktop/portal/desktop/request/{sender[1:].replace('.', '_')}/{token}"
            self.objects.append(
                self.bus.register_object(
                    path, Gio.DBusNodeInfo.new_for_xml(REQUEST_XML).interfaces[0], self._background_close
                )
            )
            invocation.return_value(GLib.Variant("(o)", (path,)))
            self.background_pending.append((sender, path))
            if not self.background_defer:
                self.complete_background()

    def complete_background(self):
        sender, path = self.background_pending.pop(0)
        self.bus.emit_signal(
            sender,
            path,
            "org.freedesktop.portal.Request",
            "Response",
            GLib.Variant(
                "(ua{sv})",
                (1 if self.background_denied else 0, {"background": GLib.Variant("b", not self.background_denied)}),
            ),
        )

    def _background_close(self, *_args):
        self.background_closed += 1
        _args[-1].return_value(None)

    def _notification(self, _bus, _sender, _path, _interface, method, parameters, invocation):
        values = parameters.unpack()
        if method == "AddNotification":
            self.notifications[values[1]] = values[2]
            self.history.append(dict(id=values[1], payload=values[2]))
        else:
            self.notifications.pop(values[1], None)
        invocation.return_value(None)

    def _close(self, *_args):
        self.closed = True
        _args[-1].return_value(None)

    def app_action(self, app, action):
        self.bus.call(
            app.get_application_id(),
            app.get_dbus_object_path(),
            "org.gtk.Actions",
            "Activate",
            GLib.Variant("(sava{sv})", (action, [], {})),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            None,
        )

    def activate(self, *, session=None, identifier="record"):
        self.bus.emit_signal(
            None,
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.GlobalShortcuts",
            "Activated",
            GLib.Variant("(osta{sv})", (session or self.session, identifier, 123, {})),
        )

    def close(self):
        for identifier in self.objects:
            self.bus.unregister_object(identifier)
        for identifier in self.owners:
            Gio.bus_unown_name(identifier)
