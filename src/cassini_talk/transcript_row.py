# SPDX-License-Identifier: GPL-3.0-or-later
"""Real row body plus independently allocated, nonvisual accessible actions."""

from gi.repository import Gtk


class ExactTurnRow(Gtk.Widget):
    def __init__(self):
        super().__init__(margin_bottom=24, focusable=True, accessible_role=Gtk.AccessibleRole.LIST_ITEM)
        self.body = Gtk.Box(spacing=16, accessible_role=Gtk.AccessibleRole.GROUP)
        self.body.set_parent(self)

    def append(self, child):
        self.body.append(child)

    def get_first_child(self):
        return self.body.get_first_child()

    def get_last_child(self):
        return self.body.get_last_child()

    def do_get_request_mode(self):
        return self.body.get_request_mode()

    def do_measure(self, orientation, for_size):
        return self.body.measure(orientation, for_size)

    def do_size_allocate(self, width, height, baseline):
        self.body.allocate(width, height, baseline, None)

    def do_snapshot(self, snapshot):
        self.snapshot_child(self.body, snapshot)

    def close(self):
        child = Gtk.Widget.get_first_child(self)
        while child:
            following = child.get_next_sibling()
            child.unparent()
            child = following
