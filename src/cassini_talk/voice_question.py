# SPDX-License-Identifier: GPL-3.0-or-later
"""D103 §3 (L-60): the one voices question, over the first meeting with speakers.

`Remember voices` keeps D-977's answer (both optional models are requested,
Tell speakers apart first) and both answers set the default of “Remember this
voice” in Name a speaker (`remember-voices`, “Remember people I name” in
Preferences › Transcription). Nothing is downloaded on `Not now` or Escape.
"""

from gi.repository import Adw, Gtk

from .strings import text


class VoiceQuestion(Adw.AlertDialog):
    def __init__(self, app):
        super().__init__(heading=text("voices.setup.title"), body=text("voices.setup.body.desktop"))
        self.app = app
        self.answered = None
        self.dismissed = False
        self.footnote = Gtk.Label(
            label=text("voices.setup.kindness") + " " + text("voices.setup.changeLater.desktop"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            css_classes=["dim-label", "voice-question-footnote"],
        )
        self.set_extra_child(self.footnote)
        self.add_response("not-now", text("common.notNow"))
        self.add_response("enable", text("voices.setup.enable"))
        self.set_response_appearance("enable", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("enable")  # HARMONY §7: Return is the primary.
        self.set_close_response("not-now")  # Escape is Not now: nothing downloads.
        self.connect("response", self._response)
        self.connect("closed", lambda *_: app._voice_question_closed(self))

    def dismiss(self):
        """Close without answering: Preferences › Remember people I name already did."""
        self.dismissed = True
        self.force_close()

    def _response(self, _dialog, response):
        if self.answered is not None or self.dismissed:
            return
        if self.app.recording_settings.get_string("voice-setup") != "unanswered":
            # Answered elsewhere (Preferences) while this was open: never overwrite it
            # and never start downloads the person switched off.
            return
        self.answered = response == "enable"
        self.app.answer_voice_setup(self.answered)
        settings = self.app.settings_dialog
        if settings is not None and not settings.closed:
            settings.render_models()
