#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D41.2 private real D-Bus watcher, menu, lifecycle and no-host behavior."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

assert os.environ.get("CASSINI_SNI_TEST_ISOLATED") == "1" and not os.environ.get("WAYLAND_DISPLAY")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from gi.repository import Gio, GLib  # noqa: E402

from cassini_talk.status_notifier import ITEM, ITEM_PATH, MENU, MENU_PATH, WATCHER, StatusNotifier  # noqa: E402


def until(check, label):
    expired = False

    def timeout():
        nonlocal expired
        expired = True
        return GLib.SOURCE_REMOVE

    timer = GLib.timeout_add_seconds(5, timeout)
    context = GLib.MainContext.default()
    while not check() and not expired:
        context.iteration(True)
    if not expired:
        GLib.source_remove(timer)
    assert not expired and check(), label


def connection():
    return Gio.DBusConnection.new_for_address_sync(
        Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )


server, client, host = connection(), connection(), connection()
errors, actions, registrations = [], [], []
app = SimpleNamespace(
    get_application_id=lambda: "io.codemyriad.CassiniTalk",
    activate=lambda: actions.append("open"),
    activate_action=lambda name, _: actions.append(name),
)
application_name = app.get_application_id()
assert server.call_sync(
    "org.freedesktop.DBus",
    "/org/freedesktop/DBus",
    "org.freedesktop.DBus",
    "RequestName",
    GLib.Variant("(su)", (application_name, 0)),
    GLib.VariantType.new("(u)"),
    Gio.DBusCallFlags.NONE,
    2000,
    None,
).unpack() == (1,)
notifier = StatusNotifier(app, server)
identities = set()


def call(path, interface, method, parameters=None, *, destination=None):
    replies = []

    def received(bus, result):
        try:
            replies.append(bus.call_finish(result).unpack())
        except GLib.Error as error:
            errors.append(error)
            replies.append(None)

    client.call(
        destination or server.get_unique_name(),
        path,
        interface,
        method,
        parameters,
        None,
        Gio.DBusCallFlags.NO_AUTO_START,
        2000,
        None,
        received,
    )
    until(lambda: replies, f"reply to {method}")
    assert replies[0] is not None, errors
    return replies[0]


def discover_items():
    # Ubuntu AppIndicator scans well-known names and keys its discovered
    # items by the literal bus name + path, not the resolved unique owner.
    names = call("/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames", destination="org.freedesktop.DBus")[0]
    candidates = [name for name in names if not name.startswith(":")]
    completed = []

    def inspected(bus, result, name):
        try:
            xml = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            pass  # Most services have no item at the conventional path.
        else:
            if any(interface.name == ITEM for interface in Gio.DBusNodeInfo.new_for_xml(xml).interfaces):
                identities.add((name, ITEM_PATH))
        completed.append(name)

    for name in candidates:
        client.call(
            name,
            ITEM_PATH,
            "org.freedesktop.DBus.Introspectable",
            "Introspect",
            None,
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            2000,
            None,
            inspected,
            name,
        )
    until(lambda: len(completed) == len(candidates), "host discovery introspection replies")
    assert identities == {(application_name, ITEM_PATH)}, identities


def prop(name):
    return call(ITEM_PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (ITEM, name)))[0]


notifier.update(recording=False, windowless=True, automatic=True)
until(lambda: notifier.generation, "initial absent watcher observed")
assert not notifier.exports and not notifier.registered
watcher_xml = """<node><interface name="org.kde.StatusNotifierWatcher">
<property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
<property name="RegisteredStatusNotifierItems" type="as" access="read"/>
<property name="ProtocolVersion" type="i" access="read"/>
<method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
<signal name="StatusNotifierHostRegistered"/><signal name="StatusNotifierHostUnregistered"/>
</interface></node>"""
host_ready = False


def watch_method(_bus, sender, _path, _interface, method, parameters, invocation):
    assert method == "RegisterStatusNotifierItem"
    service = parameters.unpack()[0]
    registrations.append((sender, service))
    identities.add((sender, service) if service.startswith("/") else (service, ITEM_PATH))
    invocation.return_value(None)


def watch_property(_bus, _sender, _path, _interface, name):
    return GLib.Variant(
        *{
            "IsStatusNotifierHostRegistered": ("b", host_ready),
            "RegisteredStatusNotifierItems": ("as", [r[1] + ITEM_PATH for r in registrations]),
            "ProtocolVersion": ("i", 0),
        }[name]
    )


export = host.register_object(
    "/StatusNotifierWatcher",
    Gio.DBusNodeInfo.new_for_xml(watcher_xml).interfaces[0],
    watch_method,
    watch_property,
    None,
)
owned = Gio.bus_own_name_on_connection(host, WATCHER, Gio.BusNameOwnerFlags.NONE, None, None)
until(lambda: notifier.proxy is not None, "watcher discovered without a registered host")
assert not notifier.exports and not registrations
# Like KDE's watcher, announce hosts only with the watcher's own signals: no
# PropertiesChanged, so the proxy's cached property would otherwise stay stale.
host_ready = True
host.emit_signal(None, "/StatusNotifierWatcher", WATCHER, "StatusNotifierHostRegistered", None)
until(lambda: notifier.registered, "real asynchronous watcher registration")
assert notifier.available
host_ready = False
host.emit_signal(None, "/StatusNotifierWatcher", WATCHER, "StatusNotifierHostUnregistered", None)
until(lambda: not notifier.available, "host unregistration without PropertiesChanged")
host_ready = True
host.emit_signal(None, "/StatusNotifierWatcher", WATCHER, "StatusNotifierHostRegistered", None)
until(lambda: notifier.available, "host registration again")
assert server.get_unique_name().startswith(":")
discover_items()  # App registers first, then the host discovers its alias.
assert registrations == [(server.get_unique_name(), application_name)]
assert prop("Status") == "Active"
assert prop("IconName") == "io.codemyriad.CassiniTalk-symbolic"
revision, layout = call(MENU_PATH, MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
assert [r[1].get("label") for r in layout[2] if r[1].get("visible")] == [
    "Ready to record browser meetings",
    "Open Cassini",
    None,
    "Quit Cassini",
]
call(ITEM_PATH, ITEM, "Activate", GLib.Variant("(ii)", (0, 0)))
call(MENU_PATH, MENU, "Event", GLib.Variant("(isvu)", (3, "clicked", GLib.Variant("s", ""), 0)))
assert actions == ["open"], "hidden Stop must not stop an idle app"
notifier.update(recording=True, message="Recording, 00:00:12", label="00:00:12", windowless=False)
assert prop("IconName") == "" and prop("Status") == "Active"
assert prop("XAyatanaLabel") == "00:00:12" and prop("XAyatanaLabelGuide") == "00:00:00"
assert prop("ToolTip")[2] == "Recording, 00:00:12"
pixmap = prop("IconPixmap")[0]
assert pixmap[:2] == (24, 24) and len(pixmap[2]) == 24 * 24 * 4
call(ITEM_PATH, ITEM, "ProvideXdgActivationToken", GLib.Variant("(s)", ("test-token",)))
assert notifier.activation_token == "test-token"
call(ITEM_PATH, ITEM, "Activate", GLib.Variant("(ii)", (0, 0)))
assert app._sni_activation_token == "test-token" and not notifier.activation_token
call(
    MENU_PATH,
    MENU,
    "EventGroup",
    GLib.Variant("(a(isvu))", ([(1, "clicked", GLib.Variant("s", ""), 0), (3, "clicked", GLib.Variant("s", ""), 0)],)),
)
assert actions == ["open", "open", "stop-recording"]
changes = []
subscription = client.signal_subscribe(
    server.get_unique_name(),
    MENU,
    None,
    MENU_PATH,
    None,
    Gio.DBusSignalFlags.NONE,
    lambda _bus, _sender, _path, _interface, name, _args: changes.append(name),
)
notifier.update(recording=True, message="Recording, 00:00:12", label="00:00:12", windowless=False)
call(MENU_PATH, MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
assert changes == [], "unchanged status must not rebuild the menu"
notifier.update(recording=True, message="Recording, 00:00:13", label="00:00:13", windowless=False)
call(MENU_PATH, MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
assert changes == ["ItemsPropertiesUpdated"], "timer changes must preserve menu focus"
notifier.update(recording=False, windowless=False, automatic=True)
assert prop("Status") == "Passive", "idle icon must hide when the Library opens"
call(MENU_PATH, MENU, "Event", GLib.Variant("(isvu)", (5, "clicked", GLib.Variant("s", ""), 0)))
assert actions[-1] == "stop-and-quit"
client.signal_unsubscribe(subscription)
call(MENU_PATH, MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
assert notifier.served_revision == notifier.revision
Gio.bus_unown_name(owned)
until(lambda: notifier.proxy is None, "watcher disappearance")
assert notifier.served_revision == -1, "a new watcher's host must fetch the layout again"
# A host/shell restart forgets its items. Discovery can win the race with
# app registration this time, while the exported item remains reachable.
identities.clear()
registrations.clear()
notifier.update(recording=False, windowless=True, automatic=True)
discover_items()
owned = Gio.bus_own_name_on_connection(host, WATCHER, Gio.BusNameOwnerFlags.NONE, None, None)
until(lambda: notifier.registered, "registration after watcher restart and discovery")
assert registrations == [(server.get_unique_name(), application_name)]
assert identities == {(application_name, ITEM_PATH)}
discover_items()
Gio.bus_unown_name(owned)
until(lambda: notifier.proxy is None, "restarted watcher disappears")
notifier.close()
assert not notifier.exports and not errors
host.unregister_object(export)
for bus in (client, host, server):
    bus.close_sync(None)
print(
    "PASS optional SNI: absent watcher, absent host, one app/discovery identity in both orders and on restart, real wire layout/properties/actions, stable timer, hide and shutdown"
)
