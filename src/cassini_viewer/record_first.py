# SPDX-License-Identifier: GPL-3.0-or-later
"""RecordFirst permission explanation; never asks for a destination file."""

from gi.repository import Adw, Gtk

from .strings import text


class RecordFirst(Adw.Dialog):
    def __init__(self, app, parent):
        super().__init__(title=text("recordFirst.title"), content_width=520)
        self.app = app
        self.settings = app.recording_settings
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
            margin_start=24,
            margin_end=24,
            margin_top=24,
            margin_bottom=24,
        )
        self.set_child(content)
        content.append(Gtk.Label(label=text("recordFirst.title"), css_classes=["title-2"], wrap=True))
        responsibility = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["responsibility-card"])
        responsibility.append(Gtk.Label(label=text("recordFirst.responsibility"), xalign=0, wrap=True))
        self.dont_show = Gtk.CheckButton(
            label=text("recordFirst.dontShowAgain"), active=self.settings.get_boolean("skip-record-first")
        )
        self.dont_show.connect(
            "toggled", lambda button: self.settings.set_boolean("skip-record-first", button.get_active())
        )
        responsibility.append(self.dont_show)
        content.append(responsibility)
        content.append(
            Gtk.Label(label=text("recordFirst.willRecord"), xalign=0, css_classes=["heading", "dim-label", "small"])
        )
        for index, (icon, title, description, caption) in enumerate(
            [
                (
                    "audio-input-microphone-symbolic",
                    "common.yourMicrophone",
                    "recordFirst.mic.description",
                    "recordFirst.mic.caption",
                ),
                (
                    "video-display-symbolic",
                    "recordFirst.sound.title",
                    "recordFirst.sound.description",
                    "recordFirst.sound.caption",
                ),
            ]
        ):
            if index:
                content.append(Gtk.Separator())
            row = Gtk.Box(spacing=14)
            row.append(
                Gtk.Image(
                    icon_name=icon,
                    pixel_size=24,
                    valign=Gtk.Align.START,
                    accessible_role=Gtk.AccessibleRole.PRESENTATION,
                )
            )
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            for key, classes in [(title, ["heading"]), (description, ["dim-label"]), (caption, ["dim-label", "small"])]:
                body.append(Gtk.Label(label=text(key), xalign=0, wrap=True, css_classes=classes))
            row.append(body)
            content.append(row)
        buttons = Gtk.Box(spacing=10, homogeneous=True)
        later = Gtk.Button(label=text("common.notNow"))
        later.connect("clicked", lambda *_: self.close())
        self.start_button = Gtk.Button(label=text("recordFirst.start"), css_classes=["cassini-record"])
        start_label = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        start_label.append(Gtk.Label(label="●", accessible_role=Gtk.AccessibleRole.PRESENTATION))
        start_label.append(Gtk.Label(label=text("recordFirst.start")))
        self.start_button.set_child(start_label)
        self.start_button.update_property([Gtk.AccessibleProperty.LABEL], [text("recordFirst.start")])
        self.start_button.connect("clicked", self._start)
        buttons.append(later)
        buttons.append(self.start_button)
        content.append(buttons)
        self.set_default_widget(self.start_button)
        self.present(parent)

    def _start(self, *_):
        self.close()
        self.app.start_native()
