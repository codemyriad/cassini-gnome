# SPDX-License-Identifier: GPL-3.0-or-later
"""UX2 warning/info line with at most one action, reusable by each page."""

from gi.repository import Gtk

from .strings import text


class InlineLine(Gtk.Box):
    def __init__(self, identifier, *, action=None, callback=None, info=False, **values):
        super().__init__(spacing=12, css_classes=["inline-line", "info-line" if info else "warn-line"])
        self.append(
            Gtk.Image(
                icon_name="dialog-information-symbolic" if info else "dialog-warning-symbolic",
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        self.append(Gtk.Label(label=text(identifier, **values), xalign=0, wrap=True, hexpand=True))
        if action:
            button = Gtk.Button(label=text(action), sensitive=callback is not None)
            if callback:
                button.connect("clicked", lambda *_: callback())
            self.append(button)
