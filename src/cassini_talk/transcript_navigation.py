# SPDX-License-Identifier: GPL-3.0-or-later
"""Keyboard traversal of exact transcript rows, including unrealized rows."""

from bisect import bisect_right

from gi.repository import Gdk, Gtk


class TranscriptNavigation:
    def __init__(self, view):
        self.view = view
        self.pending = None
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.key)
        view.add_controller(keys)

    def focused(self):
        focus = self.view.get_root().get_focus() if self.view.get_root() else None
        return next(
            (i for i, row in self.view.rows.items() if focus is row or focus is not None and focus.is_ancestor(row)),
            None,
        )

    def request(self, index, direction=None):
        if not self.view.items:
            return False
        index = min(len(self.view.items) - 1, max(0, index))
        self.pending = index, direction
        self.view.page._hand_scroll()
        self.view.scroll_to(index)
        self.view.queue_allocate()
        return True

    def allocated(self):
        if self.pending and self.pending[0] in self.view.rows:
            index, direction = self.pending
            self.pending = None
            row = self.view.rows[index]
            if direction is None or not row.child_focus(direction):
                row.grab_focus()

    def key(self, _keys, key, _code, modifiers):
        if modifiers & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False  # Alt arrows belong to ListenController.
        aliases = {
            Gdk.KEY_KP_Up: Gdk.KEY_Up,
            Gdk.KEY_KP_Down: Gdk.KEY_Down,
            Gdk.KEY_KP_Left: Gdk.KEY_Left,
            Gdk.KEY_KP_Right: Gdk.KEY_Right,
            Gdk.KEY_KP_Home: Gdk.KEY_Home,
            Gdk.KEY_KP_End: Gdk.KEY_End,
            Gdk.KEY_KP_Page_Up: Gdk.KEY_Page_Up,
            Gdk.KEY_KP_Page_Down: Gdk.KEY_Page_Down,
        }
        key = aliases.get(key, key)
        current = self.focused()
        if current is None:
            current = max(0, bisect_right(self.view.prefix, self.view._value()) - 1)
        if key in (Gdk.KEY_Up, Gdk.KEY_Down):
            return self.request(current + (1 if key == Gdk.KEY_Down else -1))
        if key in (Gdk.KEY_Home, Gdk.KEY_End):
            return self.request(0 if key == Gdk.KEY_Home else len(self.view.items) - 1)
        if key in (Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            adjustment = self.view.props.vadjustment
            delta = adjustment.get_page_size() * (1 if key == Gdk.KEY_Page_Down else -1)
            index = max(0, bisect_right(self.view.prefix, adjustment.get_value() + delta) - 1)
            if key == Gdk.KEY_Page_Down and index <= current:
                index = current + 1
            return self.request(index)
        if key in (Gdk.KEY_Left, Gdk.KEY_Right):
            # Keep GtkLabel's own per-word/link focus traversal.
            focus = self.view.get_root().get_focus()
            if isinstance(focus, Gtk.Label):
                return False
            row = self.view.rows.get(current)
            return bool(
                row and row.child_focus(Gtk.DirectionType.LEFT if key == Gdk.KEY_Left else Gtk.DirectionType.RIGHT)
            )
        return False

    def focus(self, direction):
        previous = self.focused()
        if (
            previous is None
            and self.view.visible_rows
            and direction
            in (
                Gtk.DirectionType.TAB_FORWARD,
                Gtk.DirectionType.TAB_BACKWARD,
            )
        ):
            index = self.view.visible_rows[0 if direction == Gtk.DirectionType.TAB_FORWARD else -1]
            return self.request(index)
        if Gtk.Widget.do_focus(self.view, direction):
            current = self.focused()
            if (
                current is not None
                and current != previous
                and direction in (Gtk.DirectionType.TAB_FORWARD, Gtk.DirectionType.TAB_BACKWARD)
            ):
                self.view.page._hand_scroll()
                self.view.scroll_to(current)
            return True
        if direction not in (Gtk.DirectionType.TAB_FORWARD, Gtk.DirectionType.TAB_BACKWARD):
            return False
        current = self.focused()
        if current is None:
            current = previous
        if current is None:
            return self.request(0, direction)
        next_row = current + (1 if direction == Gtk.DirectionType.TAB_FORWARD else -1)
        return 0 <= next_row < len(self.view.items) and self.request(next_row, direction)
