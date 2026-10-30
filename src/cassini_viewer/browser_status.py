# SPDX-License-Identifier: GPL-3.0-or-later
"""Quiet D31 state/actions in the Library's browser area."""

from gi.repository import Gtk

from .browser_connection import add_text, state_text
from .strings import text


class BrowserStatus(Gtk.Box):
    def __init__(self, app, parent):
        super().__init__(spacing=8, margin_start=16, margin_end=16, margin_top=8, margin_bottom=8)
        self.app = app
        self.message = Gtk.Label(xalign=0, wrap=True, hexpand=True, css_classes=["caption", "dim-label"])
        self.append(self.message)
        self.add = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.add.connect("clicked", lambda *_: app.on_companion_add(parent=parent))
        self.append(self.add)
        self.retry = Gtk.Button(label=text("common.tryAgain"), valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.retry.connect("clicked", app.retry_browser_connection)
        self.append(self.retry)
        self.render()

    def render(self):
        controller = self.app.browser_connection
        state = controller.snapshot() if controller else None
        self.set_visible(
            bool(
                state
                and self.app.browser_companion_available
                and state.get("everConnected")
                and state["phase"] != "needsBrowser"
            )
        )
        if not controller:
            return
        state = controller.snapshot()
        self.message.set_label(state_text(state))
        self.message.set_css_classes(["caption", "warning" if state["phase"] == "failed" else "dim-label"])
        self.add.set_label(add_text(state))
        self.add.set_visible(bool(state["phase"] == "waiting" and state["browser"]))
        self.retry.set_visible(state["phase"] == "failed")
