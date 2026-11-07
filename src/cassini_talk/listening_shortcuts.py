# SPDX-License-Identifier: GPL-3.0-or-later
"""Page-local listening actions: descendants and transients claim keys first."""

from gi.repository import Gdk, Gio, Gtk


class ListeningShortcuts:
    def __init__(self, page):
        self.page = page
        self.actions = Gio.SimpleActionGroup()
        self._key_claimed = False
        capture = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        capture.connect("key-pressed", self._key_pressed)
        capture.connect("key-released", self._key_released)
        page.add_controller(capture)
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
        page.add_controller(controller)
        sidebar = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        sidebar.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.KeyvalTrigger.new(Gdk.KEY_F9, Gdk.ModifierType(0)),
                Gtk.CallbackAction.new(lambda *_: self.toggle_sidebar()),
            )
        )
        page.add_controller(sidebar)
        self.refresh()

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
        if name == "stop" and page.split.get_collapsed() and not page.split.get_show_content():
            page.split.set_show_content(True)
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
        if self._claimed() or not self.page.split.get_collapsed():
            return False
        self.page.split.set_show_content(not self.page.split.get_show_content())
        return True
