# SPDX-License-Identifier: GPL-3.0-or-later
"""☰ › Help (F1): the offline in-app Help view (D100.5, HARMONY §10, §12).

Four short catalog topics and a Keyboard shortcuts button. It opens no URL and uses no
network: every word comes from the bundled string catalog.
"""

from gi.repository import Adw, Gtk

from .strings import text

TOPICS = ("record", "library", "trash", "voices")


def record_shortcut(app):
    shortcut = getattr(app, "global_shortcut", None)
    description = getattr(shortcut, "trigger_description", None)
    return description or text("common.shortcut.recordAnywhere")


def topic_body(app, topic):
    if topic == "record":
        return text("help.record.body", shortcut=record_shortcut(app))
    return text(f"help.{topic}.body")


class HelpDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title=text("help.title"), content_width=520, content_height=560)
        self.window = window
        app = window.get_application()
        page = Adw.PreferencesPage()
        self.topics = []
        for topic in TOPICS:
            group = Adw.PreferencesGroup(title=text(f"help.{topic}.title"))
            body = Gtk.Label(label=topic_body(app, topic), wrap=True, xalign=0)
            body.add_css_class("body")
            group.add(body)
            page.add(group)
            self.topics.append((group, body))
        self.shortcuts_button = Gtk.Button(
            label=text("menu.keyboardShortcuts"), halign=Gtk.Align.CENTER, css_classes=["pill"]
        )
        self.shortcuts_button.connect("clicked", self._shortcuts)
        footer = Adw.PreferencesGroup()
        footer.add(self.shortcuts_button)
        page.add(footer)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)

    def _shortcuts(self, *_args):
        app = self.window.get_application()
        self.close()
        app.activate_action("shortcuts", None)
