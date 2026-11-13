# SPDX-License-Identifier: GPL-3.0-or-later
"""Page-local listening actions: descendants and transients claim keys first."""

from gi.repository import Gdk, Gio, Gtk


class ListeningShortcuts:
    def __init__(self, page):
        self.page = page
        split = getattr(page.window, "split", None)
        self.split = None if page.window._external_meeting else split
        self.actions = Gio.SimpleActionGroup()
        self._key_claimed = False
        self._pending_page_focus = False
        capture = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        capture.connect("key-pressed", self._key_pressed)
        capture.connect("key-released", self._key_released)
        controller = Gtk.ShortcutController(
            scope=Gtk.ShortcutScope.LOCAL, propagation_phase=Gtk.PropagationPhase.BUBBLE
        )
        for name, key, modifiers in (
            ("next-turn", Gdk.KEY_Down, Gdk.ModifierType.ALT_MASK),
            ("previous-turn", Gdk.KEY_Up, Gdk.ModifierType.ALT_MASK),
            ("next-speaker", Gdk.KEY_Down, Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK),
            ("previous-speaker", Gdk.KEY_Up, Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK),
            ("stop", Gdk.KEY_Escape, Gdk.ModifierType(0)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, name=name: self.perform(name))
            self.actions.add_action(action)
            callback = Gtk.CallbackAction.new(lambda *_args, name=name: self.perform(name))
            controller.add_shortcut(Gtk.Shortcut.new(Gtk.KeyvalTrigger.new(key, modifiers), callback))
        page.insert_action_group("listen", self.actions)
        self._page_controllers = [capture, controller]
        if self.split is None:
            sidebar = Gtk.ShortcutController(
                scope=Gtk.ShortcutScope.LOCAL, propagation_phase=Gtk.PropagationPhase.CAPTURE
            )
            sidebar.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.KeyvalTrigger.new(Gdk.KEY_F9, Gdk.ModifierType(0)),
                    Gtk.CallbackAction.new(lambda *_: self.toggle_sidebar()),
                )
            )
            self._page_controllers.append(sidebar)
        for item in self._page_controllers:
            page.add_controller(item)
        self._map_handler = page.connect("map", self._on_page_map)
        self._split_host = None if self.split is None else (getattr(page.window, "breakpoint_bin", None) or self.split)
        self._split_controllers = []
        if self._split_host is not None:
            split_capture = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            split_capture.connect("key-pressed", self._key_pressed)
            split_capture.connect("key-released", self._key_released)
            split_f9 = Gtk.ShortcutController(
                scope=Gtk.ShortcutScope.MANAGED, propagation_phase=Gtk.PropagationPhase.CAPTURE
            )
            split_f9.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.KeyvalTrigger.new(Gdk.KEY_F9, Gdk.ModifierType(0)),
                    Gtk.CallbackAction.new(lambda *_: self.toggle_sidebar()),
                )
            )
            split_escape = Gtk.ShortcutController(
                scope=Gtk.ShortcutScope.MANAGED, propagation_phase=Gtk.PropagationPhase.CAPTURE
            )
            split_escape.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.KeyvalTrigger.new(Gdk.KEY_Escape, Gdk.ModifierType(0)),
                    Gtk.CallbackAction.new(lambda *_: self._sidebar_escape()),
                )
            )
            self._split_controllers = [split_capture, split_f9, split_escape]
            for item in self._split_controllers:
                self._split_host.add_controller(item)
        self.refresh()

    def close(self):
        if self._map_handler:
            self.page.disconnect(self._map_handler)
            self._map_handler = 0
        for item in self._page_controllers:
            self.page.remove_controller(item)
        self._page_controllers.clear()
        if self._split_host is not None:
            for item in self._split_controllers:
                self._split_host.remove_controller(item)
            self._split_controllers.clear()

    def _focus_into_page(self):
        target = self.page.listening_bar.stop if self.page.window.listen.active else self.page.play
        if target.get_mapped() and target.is_sensitive() and target.grab_focus():
            self._pending_page_focus = False
            return
        self._pending_page_focus = True

    def _on_page_map(self, *_):
        if self._pending_page_focus:
            self._pending_page_focus = False
            target = self.page.listening_bar.stop if self.page.window.listen.active else self.page.play
            if target.get_mapped() and target.is_sensitive():
                target.grab_focus()

    def _sidebar_escape(self):
        if (
            self.split is None
            or not self.split.get_mapped()
            or not self.split.get_collapsed()
            or self.split.get_show_content()
        ):
            return False
        if self.page.window.get_visible_dialog() or self._key_claimed or self._claimed():
            return False
        self.split.set_show_content(True)
        self._focus_into_page()
        return True

    def _key_pressed(self, *_):
        # GtkEntry can move focus while handling Escape/Alt+Down. Preserve who
        # owned this event before descendant handling, without consuming it.
        self._key_claimed = self._claimed()
        return False

    def _key_released(self, *_):
        self._key_claimed = False

    def _claimed(self):
        window = self.page.window
        if window._text_focused() or window.get_visible_dialog():
            return True
        widget = window.get_focus()
        while widget:
            if isinstance(widget, Gtk.Popover):
                return True
            widget = widget.get_parent()
        return False

    def refresh(self):
        enabled = self.page.window.listen.active and not self._claimed()
        for name in self.actions.list_actions():
            self.actions.lookup_action(name).set_enabled(enabled)

    def perform(self, name):
        if self.page.window.get_visible_dialog():
            return False
        if self._key_claimed or self.page.window._text_focused():
            # Descendants have already had Escape in the bubble phase. If an
            # entry leaves it unhandled, consume it here without changing
            # listening; otherwise AdwNavigationView would pop this page.
            return name == "stop"
        if self._claimed():
            return False
        page, listen = self.page, self.page.window.listen
        split = getattr(page.window, "split", None)
        if name == "stop" and split is not None and split.get_collapsed() and not split.get_show_content():
            split.set_show_content(True)
            return True
        if not listen.active:
            return False
        if name == "stop":
            page.stop_listening()
            return True
        if name == "next-turn":
            listen.next_turn()
        elif name == "previous-turn":
            listen.previous_turn()
        elif name == "next-speaker":
            page.listening_bar.step_speaker(1)
        elif name == "previous-speaker":
            page.listening_bar.step_speaker(-1)
        else:
            return False
        # At an end there is no step, but Alt+arrow must not fall through to
        # GTK focus traversal and move focus away from the listening controls.
        return True

    def toggle_sidebar(self):
        split = self.split
        if (
            self.page.window.get_visible_dialog()
            or self._claimed()
            or split is None
            or not split.get_mapped()
            or not split.get_collapsed()
        ):
            return False
        show_content = not split.get_show_content()
        split.set_show_content(show_content)
        if show_content:
            self._focus_into_page()
        else:
            rows = getattr(self.page.window, "rows", None)
            if rows is not None:
                row = rows.get_selected_row() or rows.get_first_child()
                if row is not None and row.get_mapped():
                    row.grab_focus()
        return True
