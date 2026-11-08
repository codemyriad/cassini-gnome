# SPDX-License-Identifier: GPL-3.0-or-later
"""D39 explicit user-driven unpacked installation; connection is the only proof."""

import logging
import threading

from gi.repository import Adw, GLib, Gtk

from . import companion
from .browser_connection import state_text
from .browser_health import setup_complete
from .strings import text

LOG = logging.getLogger(__name__)


class LocalCompanionDialog(Adw.Dialog):
    def __init__(self, setup, browser, folder):
        super().__init__(title=text("companion.local.title"), content_width=540, follows_content_size=True)
        self.setup, self.app, self.browser, self.folder = setup, setup.app, browser, folder
        self.closed = self.opening = self.failed = False
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=24,
            margin_end=24,
            margin_top=24,
            margin_bottom=24,
        )
        self.toasts = Adw.ToastOverlay(child=content)
        self.set_child(self.toasts)
        content.append(Gtk.Label(label=text("companion.local.title"), wrap=True, css_classes=["title-2"]))
        content.append(Gtk.Label(label=text("companion.local.step1", browser=browser.name), wrap=True, xalign=0))
        self.address_button = Gtk.Button(
            label=text("companion.local.copyExtensions", browser=browser.name), halign=Gtk.Align.START
        )
        self.address_button.connect("clicked", self._copy_extensions)
        content.append(self.address_button)
        content.append(
            Gtk.Label(
                label=text("companion.local.extensionsPasteHint"),
                wrap=True,
                xalign=0,
                css_classes=["caption", "dim-label"],
            )
        )
        self.problem = Gtk.Label(wrap=True, xalign=0, visible=False, css_classes=["warn-line"])
        content.append(self.problem)
        from .companion_local_instructions import LocalCompanionInstructions

        self.instructions = LocalCompanionInstructions(
            lambda: self.toasts.add_toast(Adw.Toast.new(text("companion.local.copied")))
        )
        self.instructions.set_folder(folder)
        self.copy_button = self.instructions.copy_button
        content.append(self.instructions)
        content.append(
            Gtk.Label(
                label=text("companion.local.devModeNote", browser=browser.name),
                wrap=True,
                xalign=0,
                css_classes=["caption", "dim-label"],
            )
        )
        self.status = Gtk.Label(wrap=True, xalign=0, css_classes=["caption", "dim-label"])
        content.append(self.status)
        actions = Gtk.Box(spacing=12, homogeneous=True)
        later = Gtk.Button(label=text("common.notNow"))
        later.connect("clicked", lambda *_: self.close())
        actions.append(later)
        self.done = Gtk.Button(label=text("common.done"), css_classes=["suggested-action"])
        self.done.connect("clicked", self._done)
        actions.append(self.done)
        content.append(actions)
        self.set_default_widget(self.address_button)
        self.connect("closed", lambda *_: setattr(self, "closed", True))
        self.render()

    def _copy_extensions(self, *_):
        if self.opening or self.closed:
            return
        self.get_display().get_clipboard().set(companion.extensions_address(self.browser))
        self.toasts.add_toast(Adw.Toast.new(text("companion.local.extensionsCopied")))
        self.opening, self.failed = True, False
        self.render()

        def work():
            try:
                process = companion.open_new_tab(self.browser)
                companion.check_browser_launch(process)
                error = None
            except Exception as problem:
                LOG.warning("Could not open the browser: %s", problem)
                error = problem
            GLib.idle_add(self._opened, error)

        threading.Thread(target=work, name="cassini-open-extensions", daemon=True).start()

    def _opened(self, error):
        if not self.closed:
            self.opening, self.failed = False, error is not None
            self.render()
        return GLib.SOURCE_REMOVE

    def _done(self, *_):
        if self.done.get_sensitive():
            self.force_close()
            self.setup.force_close()

    def render(self):
        controller = self.app.browser_connection
        state = controller.snapshot() if controller else None
        connected = bool(state and state["phase"] == "connected")
        self.done.set_sensitive(connected and setup_complete(state.get("peers", ())))
        self.status.set_label(state_text(state))
        self.address_button.set_sensitive(not self.opening and bool(state and state["browser"]))
        self.address_button.set_label(
            text("common.tryAgain" if self.failed else "companion.local.copyExtensions", browser=self.browser.name)
        )
        self.problem.set_visible(self.failed)
        if self.failed:
            self.problem.set_label(text("companion.local.openFailed", browser=self.browser.name))
