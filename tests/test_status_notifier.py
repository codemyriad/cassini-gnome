# SPDX-License-Identifier: GPL-3.0-or-later
"""D41.2 menu contract and bus reply shapes; private-bus watcher test is in UI."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi.repository.Gio")
from gi.repository import Gio, GLib

from cassini_viewer.status_notifier import ITEM_XML, MENU_XML, StatusNotifier, menu_rows, recording_pixels


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
    return notifier, calls


def test_actions_do_not_stop_an_idle_app_or_activate_status_and_separator():
    notifier, calls = item()
    for identifier in (1, 3, 4):
        assert notifier._event(identifier, "clicked")
    assert not calls and not notifier._event(99, "clicked")
    notifier._event(2, "clicked")
    notifier._event(5, "clicked")
    assert calls == ["open", "quit"]
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
