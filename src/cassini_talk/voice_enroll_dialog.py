# SPDX-License-Identifier: GPL-3.0-or-later
"""D-977 `Add a saved voice`: a name plus the guided test phrase (`voices.enroll.*`).

Recording, the speech check and the CAM++ print run off the GTK thread; the
take is held in memory and in one private cache file that is deleted as soon
as the print exists. Only the print is kept, in the private voices store.
"""

import logging
from concurrent.futures import CancelledError

from gi.repository import Adw, GLib, Gtk

from .phrase_recorder import PhraseRecorder
from .speaker_edits import name_value
from .strings import text
from .voice_enroll import extract_print, judge
from .voices_logic import name_candidates

LOG = logging.getLogger(__name__)
IDLE, LISTENING, CHECKING, TOO_SHORT, READY, SAVING, FAILED = (
    "idle",
    "listening",
    "checking",
    "too-short",
    "ready",
    "saving",
    "failed",
)


class VoiceEnrollDialog(Adw.Dialog):
    def __init__(self, settings, *, recorder_factory=None, extract=None):
        super().__init__(title=text("voices.enroll.title"), content_width=520, follows_content_size=True)
        self.add_css_class("speakers-dialog")
        self.add_css_class("voice-enroll-dialog")
        self.settings, self.app = settings, settings.app
        self.recorder_factory = recorder_factory or PhraseRecorder
        self.extract = extract or extract_print
        self.recorder = None
        self.state = IDLE
        self.print = None
        self.generation = None
        self.chosen_voice = None
        self.declined_duplicate = None
        self.candidate = None
        self.closed = False
        self.saved = None
        self.connect("closed", self._closed)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=6,
            margin_bottom=20,
            margin_start=24,
            margin_end=24,
        )
        content.append(Gtk.Label(label=text("voices.enroll.instructions"), wrap=True, xalign=0))
        field = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        name_label = Gtk.Label(label=text("speakers.nameLabel"), xalign=0, css_classes=["speaker-section-label"])
        self.entry = Gtk.Entry(hexpand=True, max_length=64, css_classes=["speaker-name-entry"])
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.nameLabel")])
        self.entry.connect("changed", lambda *_: self._name_changed())
        field.append(name_label)
        field.append(self.entry)
        content.append(field)
        self.duplicate = Gtk.Box(spacing=8, visible=False, css_classes=["speaker-duplicate"])
        self.duplicate_title = Gtk.Label(
            xalign=0, wrap=True, hexpand=True, valign=Gtk.Align.CENTER, css_classes=["speaker-duplicate-text"]
        )
        self.someone_else = Gtk.Button(
            label=text("speakers.duplicate.someoneElse"),
            can_shrink=True,
            valign=Gtk.Align.CENTER,
            css_classes=["speaker-btn-secondary", "speaker-btn-small"],
        )
        self.same_person = Gtk.Button(
            label=text("speakers.duplicate.samePerson"),
            can_shrink=True,
            valign=Gtk.Align.CENTER,
            css_classes=["suggested-action", "speaker-btn-primary", "speaker-btn-small"],
        )
        self.someone_else.connect("clicked", lambda *_: self._answer_duplicate(False))
        self.same_person.connect("clicked", lambda *_: self._answer_duplicate(True))
        self.duplicate.append(self.duplicate_title)
        self.duplicate.append(self.someone_else)
        self.duplicate.append(self.same_person)
        content.append(self.duplicate)
        phrase_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        phrase_box.append(
            Gtk.Label(label=text("voices.enroll.testPhraseLabel"), xalign=0, css_classes=["speaker-section-label"])
        )
        self.phrase = Gtk.Label(
            label=text("voices.enroll.testPhrase"),
            wrap=True,
            xalign=0,
            selectable=True,
            css_classes=["speaker-dialog-quote", "voice-enroll-phrase"],
        )
        phrase_box.append(self.phrase)
        content.append(phrase_box)
        take = Gtk.Box(spacing=12)
        self.record = Gtk.Button(label=text("voices.enroll.record"), valign=Gtk.Align.CENTER)
        self.record.connect("clicked", lambda *_: self._toggle_record())
        take.append(self.record)
        self.status = Gtk.Label(xalign=0, wrap=True, hexpand=True, valign=Gtk.Align.CENTER)
        take.append(self.status)
        content.append(take)
        self.level = Gtk.LevelBar(min_value=0, max_value=1, visible=False)
        self.level.update_property([Gtk.AccessibleProperty.LABEL], [text("voices.enroll.recording")])
        content.append(self.level)
        self.error = Gtk.Label(
            label=text("savedVoices.changeFailed"), xalign=0, wrap=True, visible=False, css_classes=["error-line"]
        )
        content.append(self.error)
        content.append(
            Gtk.Label(
                label=text("voices.enroll.fromTranscriptHint"),
                wrap=True,
                xalign=0,
                css_classes=["dim-label", "caption"],
            )
        )
        footer = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.cancel = Gtk.Button(label=text("common.cancel"))
        self.cancel.connect("clicked", lambda *_: self.close())
        self.save = Gtk.Button(label=text("voices.enroll.save"), css_classes=["suggested-action"])
        self.save.connect("clicked", lambda *_: self._save())
        footer.append(self.cancel)
        footer.append(self.save)
        content.append(footer)
        toolbar.set_content(
            Gtk.ScrolledWindow(child=content, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        )
        self.set_child(toolbar)
        self.set_focus(self.entry)
        self._render()

    # Name and duplicate card (D25 §3: full name or first word, Same person / Someone else).

    def _name(self):
        try:
            return name_value(self.entry.get_text())
        except ValueError:
            return None

    def _name_changed(self):
        name = self._name()
        voices = self.app.voice_store.snapshot()["voices"]
        found = sorted(name_candidates(name or "", voices), key=lambda v: (v["name"], v["id"]))
        self.candidate = found[0] if found else None
        if self.chosen_voice and (self.candidate is None or self.chosen_voice != self.candidate["id"]):
            self.chosen_voice = None
        if self.declined_duplicate and (self.candidate is None or self.declined_duplicate != self.candidate["id"]):
            self.declined_duplicate = None
        self._render()

    def _answer_duplicate(self, same):
        if self.candidate is None:
            return
        if same:
            self.chosen_voice, self.declined_duplicate = self.candidate["id"], None
        else:
            self.chosen_voice, self.declined_duplicate = None, self.candidate["id"]
        self._render()

    def _duplicate_open(self):
        return (
            self.candidate is not None
            and self.chosen_voice != self.candidate["id"]
            and self.declined_duplicate != self.candidate["id"]
        )

    # The take.

    def _toggle_record(self):
        if self.state == LISTENING:
            self.recorder.stop()
            return
        if self.state in (CHECKING, SAVING) or self.app.recording_active():
            return
        for window in self.app.get_windows():
            if hasattr(window, "player"):
                window.player.pause()
        if getattr(self.settings, "saved_voices_page", None) is not None:
            self.settings.saved_voices_page._stop_sample()
        self.print = None
        self.error.set_visible(False)
        self.generation = self.app.voice_store.snapshot()["generation"]
        self.recorder = self.recorder_factory()
        self.recorder.connect("level", self._on_level)
        self.recorder.connect("finished", self._on_finished)
        self.state = LISTENING
        self._render()
        self.recorder.start()

    def _on_level(self, _recorder, dbfs):
        self.level.set_value(max(0.0, min(1.0, (dbfs + 60) / 60)))

    def _on_finished(self, recorder, ok):
        if self.closed or recorder is not self.recorder:
            return
        samples = recorder.samples
        self.recorder = None
        if not ok or not len(samples):
            self.state = FAILED if not ok else TOO_SHORT
            self._render()
            return
        self.state = CHECKING
        self._render()
        generation = self.generation

        def work():
            windows, enough = judge(samples)
            if not enough:
                return None
            return self.extract(self.app, samples, windows)

        future = self.settings.executor.submit(work)
        future.add_done_callback(lambda f: GLib.idle_add(self._checked, f, generation))

    def _checked(self, future, generation):
        if self.closed:
            return False
        try:
            result = future.result()
        except CancelledError:
            return False
        except (OSError, ValueError, RuntimeError):
            LOG.exception("Could not prepare the voice sample")
            self.state = IDLE
            self.error.set_visible(True)
        else:
            if generation != self.generation:
                return False
            self.print = result
            self.state = READY if result is not None else TOO_SHORT
        self._render()
        return False

    # Save.

    def _save(self):
        name = self._name()
        if self.state != READY or not name or self._duplicate_open() or self.settings.closed:
            return
        self.state = SAVING
        self.error.set_visible(False)
        self._render()
        sample, generation, voice_id = self.print, self.generation, self.chosen_voice

        def write():
            return self.app.voice_store.add_sample(
                name, sample["embedding"], sample["seconds"], generation, voice_id=voice_id
            )

        future = self.settings.executor.submit(write)
        future.add_done_callback(lambda f: GLib.idle_add(self._written, f))

    def _written(self, future):
        if self.closed:
            return False
        try:
            self.saved = future.result()
        except CancelledError:
            return False
        except (OSError, ValueError, KeyError):
            LOG.exception("Could not save the voice sample")
            self.state = READY
            self.error.set_visible(True)
            self._render()
            return False
        if self.saved is None:
            # Forget everyone (or Forget) ran during the take: never revive it.
            self.print, self.state = None, IDLE
            self._render()
            return False
        self.settings.render_voices()
        self.close()
        return False

    def _render(self):
        name = self._name()
        readonly = self.app.voice_store.snapshot()["readOnly"]
        duplicate = self._duplicate_open()
        self.duplicate.set_visible(duplicate)
        if duplicate:
            self.duplicate_title.set_text(text("speakers.duplicate.notice", name=self.candidate["name"]))
        listening = self.state == LISTENING
        self.record.set_label(text("common.stop") if listening else text("voices.enroll.record"))
        self.record.set_css_classes(["destructive-action"] if listening else [])
        self.record.set_sensitive(
            not readonly and self.state not in (CHECKING, SAVING) and (listening or not self.app.recording_active())
        )
        self.level.set_visible(listening)
        status = {
            LISTENING: text("voices.enroll.recording"),
            TOO_SHORT: text("voices.enroll.tooShort"),
            READY: text("voices.enroll.ready"),
            SAVING: text("voices.enroll.ready"),
            FAILED: text("errors.deviceUnavailable.me"),
        }.get(self.state, "")
        if readonly:
            status = text("savedVoices.newerVersion")
        self.status.set_text(status)
        self.status.set_css_classes(["error-line"] if self.state in (TOO_SHORT, FAILED) else ["dim-label"])
        self.save.set_sensitive(self.state == READY and bool(name) and not duplicate and not readonly)

    def _closed(self, *_):
        self.closed = True
        if self.recorder is not None:
            self.recorder.cancel()
            self.recorder = None
        self.print = None
