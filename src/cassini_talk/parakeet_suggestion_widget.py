# SPDX-License-Identifier: GPL-3.0-or-later
"""The same non-modal D78 recommendation in Settings and after first writing."""

from gi.repository import Gtk

from .parakeet_model import DOWNLOAD_BYTES
from .strings import text
from .ux_formats import size


class ParakeetSuggestion(Gtk.Box):
    """A calm, dismissible card, styled like the "Meet in your browser?" card.

    It always sits inside content, below a header bar (D-1060). In Settings the
    Parakeet Ultra row right above it already carries Download, so the card
    there offers only "Not now" (``download=False``) instead of repeating it.
    """

    def __init__(self, app, *, download=True):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            visible=False,
            css_classes=["suggestion-card", "parakeet-suggestion"],
        )
        self.app = app
        title = text("models.parakeetSuggestion.title")
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.append(Gtk.Label(label=title, xalign=0, wrap=True, css_classes=["heading"]))
        self.append(
            Gtk.Label(label=text("models.parakeetSuggestion.body", size=size(DOWNLOAD_BYTES)), xalign=0, wrap=True)
        )
        actions = Gtk.Box(spacing=12)
        self.download = None
        if download:
            self.download = Gtk.Button(
                label=text("models.parakeetSuggestion.download"), css_classes=["suggested-action"]
            )
            self.download.connect("clicked", self._download)
            actions.append(self.download)
        self.dismiss = Gtk.Button(label=text("common.notNow"), css_classes=["flat"])
        self.dismiss.connect("clicked", self._dismiss)
        actions.append(self.dismiss)
        self.append(actions)

    def _download(self, *_):
        if self.app.speech_models:
            self.app.speech_models.download()
        self.app._models_changed()

    def _dismiss(self, *_):
        self.app.recording_settings.set_boolean("parakeet-suggestion-dismissed", True)
        self.app._models_changed()
