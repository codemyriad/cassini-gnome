# SPDX-License-Identifier: GPL-3.0-or-later
"""D41.2 menu contract and bus reply shapes; private-bus watcher test is in UI."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi.repository.Gio")
from gi.repository import Gio, GLib

from cassini_talk.status_notifier import (
    ITEM,
    ITEM_PATH,
    ITEM_XML,
    MENU_XML,
    StatusNotifier,
    changed_properties,
    menu_rows,
    recording_pixels,
)


def test_idle_and_recording_menus_keep_ids_and_only_offer_real_stop():
    idle = menu_rows("Ready to record browser meetings", False)
    live = menu_rows("Recording, 01:23", True)
    assert idle[1] == dict(label="Ready to record browser meetings", enabled=False, visible=True)
    assert live[1]["label"] == "Recording, 01:23" and not live[1]["enabled"]
    assert [live[key].get("label") for key in (2, 3, 5)] == ["Open Cassini", "Stop Recording", "Quit Cassini"]
    assert idle[2] == live[2] and idle[5] == live[5]
    assert not idle[3]["visible"] and not idle[3]["enabled"]
    assert live[3]["visible"] and live[3]["enabled"]
    assert idle[4]["type"] == "separator"


def item():
    calls = []
    notifier = StatusNotifier.__new__(StatusNotifier)
    notifier.app = SimpleNamespace(
        activate=lambda: calls.append("open"), activate_action=lambda name, _: calls.append(name)
    )
    notifier.rows = menu_rows("Ready to record browser meetings", False)
    notifier.activation_token = ""
    return notifier, calls


def test_actions_do_not_stop_an_idle_app_or_activate_status_and_separator():
    notifier, calls = item()
    for identifier in (1, 3, 4):
        assert notifier._event(identifier, "clicked")
    assert not calls and not notifier._event(99, "clicked")
    notifier._event(2, "clicked")
    notifier._event(5, "clicked")
    assert calls == ["open", "stop-and-quit"]
    notifier.rows = menu_rows("Recording, 00:01", True)
    notifier._event(3, "clicked")
    assert calls[-1] == "stop-recording"


def test_layout_supports_depth_and_property_selection_with_wire_variants():
    notifier, _ = item()
    layout = GLib.Variant("(ia{sv}av)", notifier._layout(0, -1, ["label"]))
    root, props, children = layout.unpack()
    assert root == 0 and props == {} and len(children) == 5
    assert children[1] == (2, {"label": "Open Cassini"}, [])
    assert notifier._layout(0, 0, [])[2] == []
    with pytest.raises(ValueError, match="Unknown menu item"):
        notifier._layout(99, -1, [])


def test_interfaces_parse_and_recording_icon_is_network_argb():
    assert Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0].name == "org.kde.StatusNotifierItem"
    assert Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0].name == "com.canonical.dbusmenu"
    pixels = recording_pixels()
    assert len(pixels) == 24 * 24 * 4
    assert pixels[:4] == bytes((0, 224, 27, 36))
    assert pixels[(12 * 24 + 12) * 4 :][:4] == bytes((255, 224, 27, 36))


class CachingHost:
    """Models the AppIndicator extension's dbusmenu cache (dbusMenu.js).

    On LayoutUpdated it refetches the layout with only type/children-display
    and keeps every other cached property of items whose IDs survive. Only
    ItemsPropertiesUpdated changes those properties.
    """

    def __init__(self, rows):
        self.cache = {key: dict(value) for key, value in rows.items()}
        self.signals = []

    def emit_signal(self, _destination, _path, _interface, name, parameters):
        self.signals.append(name)
        if name == "ItemsPropertiesUpdated":
            updated, removed = parameters.unpack()
            for key, values in updated:
                self.cache.setdefault(key, {}).update(values)
            for key, names in removed:
                for prop in names:
                    self.cache.get(key, {}).pop(prop, None)


def live_notifier():
    notifier = StatusNotifier.__new__(StatusNotifier)
    notifier.closed = False
    notifier.proxy = None  # _ensure() has nothing to register
    notifier.exports = [1]
    notifier.wanted = notifier.recording = False
    notifier.message = "Ready to record browser meetings"
    notifier.label = ""
    notifier.rows = menu_rows(notifier.message, False)
    notifier.revision = 0
    notifier.served_revision = 0
    notifier.bus = CachingHost(notifier.rows)
    return notifier


def test_recording_start_and_stop_reach_a_host_that_caches_item_properties():
    notifier = live_notifier()
    host = notifier.bus
    assert not host.cache[3]["visible"]
    notifier.update(recording=True, message="Recording, 00:00")
    assert "LayoutUpdated" in host.signals and "ItemsPropertiesUpdated" in host.signals
    assert host.cache[3]["visible"] and host.cache[3]["enabled"], "Stop Recording must appear"
    assert host.cache[1]["label"] == "Recording, 00:00"
    assert notifier.revision == 1
    notifier.update(recording=False, windowless=True, automatic=True)
    assert not host.cache[3]["visible"] and not host.cache[3]["enabled"]
    assert host.cache[1]["label"] == "Ready to record browser meetings"
    for key, row in notifier.rows.items():
        assert {name: host.cache[key].get(name) for name in row} == row


def test_about_to_show_requests_a_layout_the_host_has_not_fetched():
    notifier = live_notifier()
    replies = []
    invocation = SimpleNamespace(return_value=replies.append, return_dbus_error=lambda *a: replies.append(a))
    notifier._menu_method(None, None, None, None, "AboutToShow", GLib.Variant("(i)", (0,)), invocation)
    assert replies.pop().unpack() == (False,)
    notifier.update(recording=True, message="Recording, 00:00")
    notifier._menu_method(None, None, None, None, "AboutToShow", GLib.Variant("(i)", (0,)), invocation)
    assert replies.pop().unpack() == (True,)
    notifier._menu_method(None, None, None, None, "AboutToShowGroup", GLib.Variant("(ai)", ([0],)), invocation)
    assert replies.pop().unpack() == ([0], [])
    notifier._menu_method(None, None, None, None, "GetLayout", GLib.Variant("(iias)", (0, -1, [])), invocation)
    assert replies.pop().unpack()[0] == 1
    notifier._menu_method(None, None, None, None, "AboutToShow", GLib.Variant("(i)", (0,)), invocation)
    assert replies.pop().unpack() == (False,)


def test_changed_properties_lists_only_differences():
    idle = menu_rows("Ready to record browser meetings", False)
    live = menu_rows("Recording, 00:01", True)
    assert changed_properties(idle, idle) == []
    assert dict(changed_properties(idle, live)) == {
        1: {"label": "Recording, 00:01"},
        3: {"enabled": True, "visible": True},
    }


def test_watcher_host_signals_update_availability_without_properties_changed():
    """KDE-style watchers emit only StatusNotifierHost(Un)Registered."""
    cache = {"IsStatusNotifierHostRegistered": GLib.Variant("b", False)}
    proxy = SimpleNamespace(
        get_cached_property=cache.get, set_cached_property=lambda name, value: cache.__setitem__(name, value)
    )
    notifier = StatusNotifier.__new__(StatusNotifier)
    notifier.proxy, notifier.registered = proxy, True
    ensured, changes = [], []
    notifier._ensure = lambda: ensured.append(notifier.available)
    notifier.changed = lambda: changes.append(notifier.available)
    assert not notifier.available
    notifier._watcher_signal(proxy, ":1.7", "StatusNotifierHostRegistered", GLib.Variant("()", ()))
    assert notifier.available and ensured == [True] and changes == [True]
    notifier._watcher_signal(proxy, ":1.7", "StatusNotifierHostUnregistered", GLib.Variant("()", ()))
    assert not notifier.available and changes == [True, False]


def test_a_vanished_watcher_forgets_the_served_layout():
    notifier = live_notifier()
    notifier.generation, notifier.retry_timer = 0, 0
    notifier.changed = lambda: None
    notifier._vanished()
    assert notifier.served_revision == -1 and notifier.served_revision != notifier.revision


class SignalLog:
    def __init__(self):
        self.signals = []

    def emit_signal(self, _destination, path, interface, name, parameters):
        self.signals.append((path, interface, name, parameters.unpack() if parameters is not None else None))


def test_ayatana_label_shows_the_live_timer_and_clears_when_idle():
    notifier = live_notifier()
    notifier.bus = SignalLog()
    notifier.app = SimpleNamespace(get_application_id=lambda: "io.codemyriad.CassiniTalk")
    notifier.pixels = recording_pixels()
    interface = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
    assert interface.lookup_property("XAyatanaLabel") and interface.lookup_signal("XAyatanaNewLabel")

    def labels():
        return [s[3] for s in notifier.bus.signals if s[:3] == (ITEM_PATH, ITEM, "XAyatanaNewLabel")]

    def prop(name):
        return notifier._item_property(None, None, ITEM_PATH, ITEM, name).unpack()

    assert prop("XAyatanaLabel") == ""
    notifier.update(recording=True, message="Recording, 00:00:01", label="00:00:01")
    notifier.update(recording=True, message="Recording, 00:00:02", label="00:00:02")
    assert labels() == [("00:00:01", "00:00:00"), ("00:00:02", "00:00:00")]
    assert prop("XAyatanaLabel") == "00:00:02" and prop("XAyatanaLabelGuide") == "00:00:00"
    notifier.update(recording=False, message="", label="00:00:02", windowless=True, automatic=True)
    assert labels()[-1] == ("", "") and prop("XAyatanaLabel") == "", "an idle panel shows only the icon"
