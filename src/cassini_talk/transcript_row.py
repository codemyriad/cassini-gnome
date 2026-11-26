# SPDX-License-Identifier: GPL-3.0-or-later
"""Real row body plus independently allocated, nonvisual accessible actions."""

from gi.repository import Gtk


class ExactTurnRow(Gtk.Widget):
    def __init__(self, *, body=None, margin_bottom=24, fold=None):
        super().__init__(margin_bottom=margin_bottom, focusable=True, accessible_role=Gtk.AccessibleRole.LIST_ITEM)
        self.body = body if body is not None else Gtk.Box(spacing=16)
        self.body.set_accessible_role(Gtk.AccessibleRole.GROUP)
        # D112 (L-86): an optional folded-background-voices row above the turn.
        # get_first_child/get_last_child keep addressing the turn's own columns.
        self.fold = fold
        if fold is not None:
            self._outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self._outer.append(fold)
            self._outer.append(self.body)
        else:
            self._outer = self.body
        self._outer.set_parent(self)

    def append(self, child):
        self.body.append(child)

    def get_first_child(self):
        return self.body.get_first_child()

    def get_last_child(self):
        return self.body.get_last_child()

    def do_get_request_mode(self):
        return self._outer.get_request_mode()

    def do_measure(self, orientation, for_size):
        return self._outer.measure(orientation, for_size)

    def do_size_allocate(self, width, height, baseline):
        self._outer.allocate(width, height, baseline, None)

    def do_snapshot(self, snapshot):
        self.snapshot_child(self._outer, snapshot)

    def close(self):
        child = Gtk.Widget.get_first_child(self)
        while child:
            following = child.get_next_sibling()
            child.unparent()
            child = following
