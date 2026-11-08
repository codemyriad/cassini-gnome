# SPDX-License-Identifier: GPL-3.0-or-later
"""The same non-modal D78 recommendation in Settings and after first writing."""

from gi.repository import Gtk

from .parakeet_model import DOWNLOAD_BYTES
from .strings import text
from .ux_formats import size


class ParakeetSuggestion(Gtk.Box):
    def __init__(self, app):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            visible=False,
            margin_top=12,
            margin_bottom=12,
            margin_start=16,
            margin_end=16,
        )
        self.app = app
        self.append(
            Gtk.Label(label=text("models.parakeetSuggestion.title"), xalign=0, wrap=True, css_classes=["heading"])
        )
        self.append(
            Gtk.Label(label=text("models.parakeetSuggestion.body", size=size(DOWNLOAD_BYTES)), xalign=0, wrap=True)
        )
        actions = Gtk.Box(spacing=12)
        self.download = Gtk.Button(label=text("models.parakeetSuggestion.download"))
        self.dismiss = Gtk.Button(label=text("common.notNow"))
        self.download.connect("clicked", self._download)
        self.dismiss.connect("clicked", self._dismiss)
        actions.append(self.download)
        actions.append(self.dismiss)
        self.append(actions)

    def _download(self, *_):
        if self.app.speech_models:
            self.app.speech_models.download()
        self.app._models_changed()

    def _dismiss(self, *_):
        self.app.recording_settings.set_boolean("parakeet-suggestion-dismissed", True)
        self.app._models_changed()
