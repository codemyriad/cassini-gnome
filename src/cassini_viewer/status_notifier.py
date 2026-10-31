# SPDX-License-Identifier: GPL-3.0-or-later
"""D41.2 optional StatusNotifierItem and dbusmenu, using only Gio.

Interfaces: KDE kstatusnotifieritem and Ubuntu AppIndicator interfaces-xml.
No service is activated, and no extra well-known name is owned. The watcher
registers this connection's unique name and the conventional item path.
"""

import math

from gi.repository import Gio, GLib

from .strings import text

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierItem/Menu"

ITEM_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="WindowId" type="i" access="read"/>
<property name="IconThemePath" type="s" access="read"/>
<property name="Menu" type="o" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="OverlayIconName" type="s" access="read"/>
<property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionIconName" type="s" access="read"/>
<property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionMovieName" type="s" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<method name="ProvideXdgActivationToken"><arg type="s" direction="in"/></method>
<signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewToolTip"/>
<signal name="NewStatus"><arg type="s"/></signal>
</interface></node>"""
MENU_XML = """<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/>
<property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
<signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface></node>"""


def menu_rows(message, recording):
    # Stable IDs let the desktop retain selection while the timer changes.
    return {
        0: {"children-display": "submenu"},
        1: dict(label=message, enabled=False, visible=True),
        2: dict(label=text("hidden.openCassini"), enabled=True, visible=True),
        3: dict(label=text("hidden.menu.stopRecording"), enabled=recording, visible=recording),
        4: dict(type="separator", visible=True),
        5: dict(label=text("hidden.menu.quit"), enabled=True, visible=True),
    }


def changed_properties(before, after):
    """(id, {property: new value}) for every row property that differs."""
    return [
        (key, changed)
        for key, row in after.items()
        if (changed := {name: value for name, value in row.items() if before.get(key, {}).get(name) != value})
    ]


def variants(properties, names=()):
    return {
        key: GLib.Variant("b" if isinstance(value, bool) else "s", value)
        for key, value in properties.items()
        if not names or key in names
    }


def recording_pixels(size=24):
    # Network-order ARGB, antialiased without introducing a rendering library.
    pixels = bytearray()
    for y in range(size):
        for x in range(size):
            distance = math.hypot(x + 0.5 - size / 2, y + 0.5 - size / 2)
            alpha = round(255 * max(0, min(1, size * 0.32 + 0.5 - distance)))
            pixels.extend((alpha, 224, 27, 36))
    return bytes(pixels)


class StatusNotifier:
    def __init__(self, app, connection, *, changed=None):
        self.app, self.bus = app, connection
        self.changed = changed or (lambda: None)
        self.closed = self.registered = self.registering = False
        self.proxy = None
        self.owner = ""
        self.generation = self.revision = 0
        self.served_revision = -1
        self.retry_timer = 0
        self.wanted = self.recording = False
        self.message = text("hidden.status.watching")
        self.rows = menu_rows(self.message, False)
        self.exports = []
        self.cancel = Gio.Cancellable()
        self.watch = Gio.bus_watch_name_on_connection(
            connection, WATCHER, Gio.BusNameWatcherFlags.NONE, self._appeared, self._vanished
        )
        self.pixels = recording_pixels()
        self.activation_token = ""

    def update(self, *, recording, message="", windowless=False, automatic=False):
        wanted = bool(recording or (windowless and automatic))
        message = message or text("hidden.status.watching")
        previous = self.wanted, self.recording, self.message
        self.wanted, self.recording, self.message = wanted, bool(recording), message
        if previous == (self.wanted, self.recording, self.message) or self.closed:
            return
        rows, self.rows = self.rows, menu_rows(message, self.recording)
        layout_changed = previous[1] != self.recording
        if layout_changed:
            self.revision = (self.revision + 1) % (2**32)
        self._ensure()
        if not self.exports:
            return
        if layout_changed:
            self.bus.emit_signal(None, MENU_PATH, MENU, "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))
        # A host may keep its cached properties for items whose IDs survive a
        # LayoutUpdated (the AppIndicator extension re-reads only type and
        # children-display). Every changed property is therefore also sent as
        # ItemsPropertiesUpdated, e.g. Stop Recording becoming visible.
        changed = changed_properties(rows, self.rows)
        if changed:
            self.bus.emit_signal(
                None,
                MENU_PATH,
                MENU,
                "ItemsPropertiesUpdated",
                GLib.Variant("(a(ia{sv})a(ias))", ([(key, variants(value)) for key, value in changed], [])),
            )
        for signal in ("NewTitle", "NewToolTip"):
            self.bus.emit_signal(None, ITEM_PATH, ITEM, signal, None)
        if previous[1] != self.recording:
            self.bus.emit_signal(None, ITEM_PATH, ITEM, "NewIcon", None)
        if previous[0] != wanted:
            self.bus.emit_signal(None, ITEM_PATH, ITEM, "NewStatus", GLib.Variant("(s)", (self.status,)))

    @property
    def status(self):
        return "Active" if self.wanted else "Passive"

    def _appeared(self, _bus, _name, owner):
        self.generation += 1
        generation = self.generation
        self.owner = owner
        self._clear_retry()
        self.registered = self.registering = False
        self.proxy = None

        def ready(_source, result):
            try:
                proxy = Gio.DBusProxy.new_finish(result)
            except GLib.Error:
                return
            if self.closed or generation != self.generation:
                return
            self.proxy = proxy
            proxy.connect("g-properties-changed", self._watcher_changed)
            proxy.connect("g-signal", self._watcher_changed)
            self._watcher_changed()

        Gio.DBusProxy.new(
            self.bus,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            owner,
            "/StatusNotifierWatcher",
            WATCHER,
            self.cancel,
            ready,
        )

    def _vanished(self, *_):
        self.generation += 1
        self.owner = ""
        self._clear_retry()
        self.proxy = None
        self.registered = self.registering = False

        self.changed()

    @property
    def available(self):
        host = self.proxy.get_cached_property("IsStatusNotifierHostRegistered") if self.proxy else None
        return bool(self.registered and host and host.unpack())

    def _watcher_changed(self, *_):
        self._ensure()
        self.changed()

    def _clear_retry(self):
        if self.retry_timer:
            GLib.source_remove(self.retry_timer)
            self.retry_timer = 0

    def _retry(self):
        self.retry_timer = 0
        self._ensure()
        return GLib.SOURCE_REMOVE

    def _ensure(self):
        if self.closed or not self.wanted or self.proxy is None:
            return
        host = self.proxy.get_cached_property("IsStatusNotifierHostRegistered")
        if not host or not host.unpack() or self.registered or self.registering or self.retry_timer:
            return
        if not self.exports:
            for path, xml, method, prop in (
                (ITEM_PATH, ITEM_XML, self._item_method, self._item_property),
                (MENU_PATH, MENU_XML, self._menu_method, self._menu_property),
            ):
                interface = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
                self.exports.append(self.bus.register_object(path, interface, method, prop, None))
        self.registering = True
        generation = self.generation

        def registered(proxy, result):
            try:
                proxy.call_finish(result)
                ok = True
            except GLib.Error:
                ok = False
            if self.closed or generation != self.generation:
                return
            self.registering = False
            self.registered = ok
            if not ok:
                self.retry_timer = GLib.timeout_add_seconds(5, self._retry)
            self.changed()

        self.proxy.call(
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self.bus.get_unique_name(),)),
            Gio.DBusCallFlags.NO_AUTO_START,
            2000,
            self.cancel,
            registered,
        )

    def _item_property(self, _bus, _sender, _path, _interface, name):
        icon = "" if self.recording else self.app.get_application_id() + "-symbolic"
        pixmaps = [(24, 24, self.pixels)] if self.recording else []
        values = {
            "Category": ("s", "ApplicationStatus"),
            "Id": ("s", self.app.get_application_id()),
            "Title": ("s", self.message),
            "Status": ("s", self.status),
            "WindowId": ("i", 0),
            "IconThemePath": ("s", ""),
            "Menu": ("o", MENU_PATH),
            "ItemIsMenu": ("b", False),
            "IconName": ("s", icon),
            "IconPixmap": ("a(iiay)", pixmaps),
            "OverlayIconName": ("s", ""),
            "OverlayIconPixmap": ("a(iiay)", []),
            "AttentionIconName": ("s", ""),
            "AttentionIconPixmap": ("a(iiay)", []),
            "AttentionMovieName": ("s", ""),
            "ToolTip": ("(sa(iiay)ss)", (icon, pixmaps, self.message, "")),
        }
        return GLib.Variant(*values[name])

    def _menu_property(self, _bus, _sender, _path, _interface, name):
        return GLib.Variant(
            *{
                "Version": ("u", 3),
                "TextDirection": ("s", "ltr"),
                "Status": ("s", "normal"),
                "IconThemePath": ("as", []),
            }[name]
        )

    def _item_method(self, _bus, _sender, _path, _interface, method, parameters, invocation):
        if method == "ProvideXdgActivationToken":
            # A host-issued activation token belongs only to this UI action.
            self.activation_token = parameters.unpack()[0]
        elif method in ("Activate", "SecondaryActivate"):
            self._open()
        invocation.return_value(None)

    def _open(self):
        if self.activation_token:
            self.app._sni_activation_token, self.activation_token = self.activation_token, ""
        self.app.activate()

    def _layout(self, parent, depth, names):
        if parent not in self.rows:
            raise ValueError("Unknown menu item")
        children = (
            [GLib.Variant("(ia{sv}av)", self._layout(child, depth - 1, names)) for child in range(1, 6)]
            if parent == 0 and depth != 0
            else []
        )
        return parent, variants(self.rows[parent], names), children

    def _event(self, identifier, event):
        if identifier not in self.rows:
            return False
        row = self.rows[identifier]
        if event == "clicked" and row.get("enabled") and row.get("visible"):
            if identifier == 2:
                self._open()
            action = {3: "stop-recording", 5: "quit"}.get(identifier)
            if action:
                self.app.activate_action(action, None)
        return True

    def _menu_method(self, _bus, _sender, _path, _interface, method, parameters, invocation):
        args = parameters.unpack()
        try:
            if method == "GetLayout":
                result = GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(*args)))
                self.served_revision = self.revision
            elif method == "GetGroupProperties":
                ids, names = args
                result = GLib.Variant(
                    "(a(ia{sv}))",
                    ([(key, variants(self.rows[key], names)) for key in (ids or self.rows) if key in self.rows],),
                )
            elif method == "GetProperty":
                key, name = args
                result = GLib.Variant("(v)", (variants(self.rows[key])[name],))
            elif method == "Event":
                self._event(args[0], args[1])
                result = None
            elif method == "EventGroup":
                result = GLib.Variant("(ai)", ([e[0] for e in args[0] if not self._event(e[0], e[1])],))
            elif method == "AboutToShow":
                # Ask the host to refetch if it has not seen the current layout.
                result = GLib.Variant("(b)", (self.served_revision != self.revision,))
            elif method == "AboutToShowGroup":
                stale = [0] if self.served_revision != self.revision else []
                result = GLib.Variant("(aiai)", (stale, [key for key in args[0] if key not in self.rows]))
            else:
                raise ValueError("Unknown menu method")
            invocation.return_value(result)
        except (KeyError, ValueError) as error:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", str(error))

    def close(self):
        self.closed = True
        self.cancel.cancel()
        self._clear_retry()
        Gio.bus_unwatch_name(self.watch)
        for identifier in self.exports:
            self.bus.unregister_object(identifier)
        self.exports.clear()
        self.proxy = None
