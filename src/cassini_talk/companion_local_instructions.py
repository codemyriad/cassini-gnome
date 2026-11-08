# SPDX-License-Identifier: GPL-3.0-or-later
"""Visible unpacked-install help, shared by waiting setup and the local guide."""

from gi.repository import Gtk, Pango

from .strings import text


class LocalCompanionInstructions(Gtk.Box):
    def __init__(self, copied):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.folder = None
        self.copied = copied
        self.page = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.append(self.page)
        location = Gtk.Box(spacing=12)
        self.path = Gtk.Label(
            xalign=0,
            selectable=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            width_chars=1,
            max_width_chars=1,
            hexpand=True,
            css_classes=["monospace"],
        )
        location.append(self.path)
        self.copy_button = Gtk.Button(
            label=text("companion.local.copyFolder"), sensitive=False, valign=Gtk.Align.CENTER
        )
        self.copy_button.connect("clicked", self._copy)
        location.append(self.copy_button)
        self.append(location)
        self.append(Gtk.Label(label=text("companion.local.removeOlder"), xalign=0, wrap=True))
        self.steps = []
        for number, key in enumerate(("step2", "step3", "pasteHint"), 1):
            label = Gtk.Label(label=f"{number}. " + text("companion.local." + key), xalign=0, wrap=True)
            self.steps.append(label)
            self.append(label)
        self.append(Gtk.Label(label=text("companion.local.restartBrowser"), xalign=0, wrap=True))

    def set_browser(self, browser):
        self.page.set_label(text("companion.local.step1", browser=browser.name) if browser else "")
        self.page.set_visible(browser is not None)

    def set_folder(self, folder):
        self.folder = folder
        self.path.set_label(str(folder) if folder is not None else "")
        self.copy_button.set_sensitive(folder is not None)

    def _copy(self, *_):
        if self.folder is not None:
            self.get_display().get_clipboard().set(str(self.folder))
            self.copied()
