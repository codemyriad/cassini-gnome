# SPDX-License-Identifier: GPL-3.0-or-later
"""UX16 speaker naming and merge dialog; metadata IO uses the Library worker."""

import logging
import re
from concurrent.futures import CancelledError

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import speaker_edits, ux_formats
from .strings import text
from .tokens import saved_voice_slot
from .voices_logic import name_key, pick_name, ranked

LOG = logging.getLogger(__name__)


class VoiceWriteError(OSError):
    pass


class SpeakersDialog(Adw.Dialog):
    def __init__(self, page, identifier):
        super().__init__(
            title=text("speakers.dialog.a11y"),
            content_width=540,
            css_classes=["speakers-dialog"],
        )
        self.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.dialog.a11y")])
        self.page, self.window = page, page.window
        self.meeting, self.identifier = page.meeting, identifier
        self.closed = self.busy = self.sample_active = False
        self._sample_position = None
        self.future = self.retry = None
        self.timer = 0
        self.voices_loaded = False
        self.voice_data = None
        self.voice_prepared = False
        self._voices_loading = self._voices_again = False
        self.voice_state = dict(voices=[], readOnly=False)
        self.chosen_voice = None
        self.declined_duplicate = None
        self.candidate = None
        self.connect("closed", self._closed)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=20,
            margin_bottom=20,
            margin_start=24,
            margin_end=24,
        )
        self.set_child(content)
        header = Gtk.Box(spacing=12)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.heading = Gtk.Label(xalign=0, wrap=True, css_classes=["speaker-dialog-heading"])
        self.talk = Gtk.Label(xalign=0, wrap=True, css_classes=["speaker-dialog-meta"])
        details.append(self.heading)
        details.append(self.talk)
        header.append(details)
        self.play = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            valign=Gtk.Align.CENTER,
            css_classes=["circular", "flat"],
            tooltip_text=text("speakers.playSample"),
        )
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.playSample")])
        self.play.connect("clicked", self._play)
        header.append(self.play)
        content.append(header)
        self.quote = Gtk.Label(
            xalign=0,
            wrap=True,
            lines=3,
            ellipsize=Pango.EllipsizeMode.END,
            visible=False,
            css_classes=["speaker-dialog-quote"],
        )
        content.append(self.quote)
        field = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        name = Gtk.Label(label=text("speakers.nameLabel"), xalign=0, css_classes=["speaker-section-label"])
        self.entry = Gtk.Entry(hexpand=True, activates_default=True, css_classes=["speaker-name-entry"])
        self.window.protect_text_input(self.entry)
        name.set_mnemonic_widget(self.entry)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.nameLabel")])
        field.append(name)
        field.append(self.entry)
        content.append(field)
        self.duplicate = Gtk.Box(spacing=8, visible=False, css_classes=["speaker-duplicate"])
        self.duplicate_title = Gtk.Label(
            xalign=0,
            wrap=True,
            hexpand=True,
            valign=Gtk.Align.CENTER,
            css_classes=["speaker-duplicate-text"],
        )
        self.someone_else = Gtk.Button(
            label=text("speakers.duplicate.someoneElse"),
            valign=Gtk.Align.CENTER,
            css_classes=["speaker-btn-secondary", "speaker-btn-small"],
        )
        self.same_person = Gtk.Button(
            label=text("speakers.duplicate.samePerson"),
            valign=Gtk.Align.CENTER,
            css_classes=["suggested-action", "speaker-btn-primary", "speaker-btn-small"],
        )
        self.someone_else.connect("clicked", lambda *_: self._answer_duplicate(False))
        self.same_person.connect("clicked", lambda *_: self._answer_duplicate(True))
        self.duplicate.append(self.duplicate_title)
        self.duplicate.append(self.someone_else)
        self.duplicate.append(self.same_person)
        content.append(self.duplicate)
        self.voices_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.voices_heading = Gtk.Label(
            label=text("speakers.savedVoices.heading"),
            xalign=0,
            css_classes=["speaker-section-label"],
            visible=False,
        )
        self.voices_section.append(self.voices_heading)
        self.voices_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            activate_on_single_click=True,
            css_classes=["speaker-voices-list"],
            visible=False,
        )
        self.voices_list.connect("row-activated", self._voice_row_activated)
        self.voices_scroll = Gtk.ScrolledWindow(
            child=self.voices_list,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            max_content_height=138,
            css_classes=["speaker-voices-scroll"],
            visible=False,
        )
        self.voices_section.append(self.voices_scroll)
        content.append(self.voices_section)
        self.remember_card = Gtk.Box(spacing=16, css_classes=["speaker-remember-card"])
        remember_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.remember_title = Gtk.Label(
            label=text("speakers.rememberVoice.title"),
            xalign=0,
            wrap=True,
            css_classes=["speaker-remember-title"],
        )
        self.remember_description = Gtk.Label(xalign=0, wrap=True, css_classes=["speaker-remember-body"])
        self.voice_settings = Gtk.Button(
            label=text("speakers.rememberVoice.settings"),
            halign=Gtk.Align.START,
            css_classes=["flat", "accent"],
            visible=False,
        )
        self.voice_settings.connect("clicked", self._open_voice_settings)
        remember_copy.append(self.remember_title)
        remember_copy.append(self.remember_description)
        remember_copy.append(self.voice_settings)
        self.remember = Gtk.Switch(
            active=True,
            sensitive=False,
            valign=Gtk.Align.START,
            css_classes=["speaker-remember-switch"],
        )
        self.remember.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.rememberVoice")])
        self.remember.connect("notify::active", self._remember_changed)
        self.remember_card.append(remember_copy)
        self.remember_card.append(self.remember)
        content.append(self.remember_card)
        self.error = Gtk.Label(
            label=text("speakers.changeFailed"),
            xalign=0,
            wrap=True,
            visible=False,
            css_classes=["error", "error-line"],
        )
        content.append(self.error)
        footer = Gtk.Box(spacing=8, margin_top=2)
        self.merge_menu = Gio.Menu()
        self.merge = Gtk.MenuButton(
            label=text("speakers.mergeInto"),
            menu_model=self.merge_menu,
            css_classes=["speaker-btn-secondary"],
        )
        footer.append(self.merge)
        footer.append(Gtk.Box(hexpand=True))
        self.cancel = Gtk.Button(label=text("common.cancel"), css_classes=["speaker-btn-secondary"])
        self.cancel.connect("clicked", lambda *_: self.close())
        self.save = Gtk.Button(
            label=text("speakers.saveName"),
            css_classes=["suggested-action", "speaker-btn-primary"],
        )
        self.save.connect("clicked", self._save)
        footer.append(self.cancel)
        footer.append(self.save)
        content.append(footer)
        self.actions = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new("merge", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, target: self._merge(target.get_string()))
        self.actions.add_action(action)
        self.insert_action_group("speaker", self.actions)
        self.update(self.meeting)
        selected = next(s for s in self.meeting.speakers if s.id == identifier)
        # Generated Speaker n is a display placeholder, not a stored name.
        raw = next((s for s in self.meeting.metadata.get("speakers", []) if s["id"] == identifier), {})
        value = raw.get("name")
        if value is None and selected.provenance != "unnamed" and not re.fullmatch(r"Speaker \d+", selected.name):
            value = selected.name
        self.initial_name = value or ""
        self.entry.set_text(self.initial_name)
        self.entry.connect("changed", self._changed)
        self.entry.connect("map", lambda *_: GLib.idle_add(self._place_caret))
        self._validate()
        self.set_default_widget(self.save)
        self.timer = GLib.timeout_add(50, self._tick)
        self._load_voices()

    def _place_caret(self):
        if not self.closed:
            self.entry.set_position(-1)
        return GLib.SOURCE_REMOVE

    def update(self, meeting):
        self.meeting = meeting
        speaker = next((s for s in meeting.speakers if s.id == self.identifier), None)
        if speaker is None or not meeting.metadata_editable:
            self.close()
            return
        self.heading.set_label(speaker.name)
        self.talk.set_label(text("speakers.talkTime", talkTime=ux_formats.talk_time(speaker.talk_ms)))
        self.sample = speaker.sample_span
        line = speaker_edits.speaker_quote(meeting, self.identifier, self.sample)
        self.quote.set_label(text("common.quote", quote=line) if line else "")
        self.quote.set_visible(bool(line))
        self.merge_menu.remove_all()
        for target in meeting.speakers:
            if target.id != self.identifier:
                item = Gio.MenuItem.new(target.name, None)
                item.set_action_and_target_value("speaker.merge", GLib.Variant("s", target.id))
                self.merge_menu.append_item(item)
        self.merge.set_sensitive(not self.busy and len(meeting.speakers) > 1)

    def _load_voices(self):
        if self.closed or self.window.monitor._closed:
            return
        if self._voices_loading:
            self._voices_again = True
            return
        self._voices_loading = True

        def load():
            store = self.window.get_application().voice_store
            state = store.refresh()
            if state["readOnly"]:
                return state, None, False
            cache, prepared = store.preparation(self.meeting.directory)
            return state, cache["speakers"].get(self.identifier), prepared

        future = self.window.monitor._executor.submit(load)
        future.add_done_callback(lambda f: GLib.idle_add(self._voices_loaded, f))

    def _voices_loaded(self, future):
        self._voices_loading = False
        try:
            state, item, prepared = future.result()
        except CancelledError:
            return False
        except (OSError, ValueError):
            LOG.exception("Could not load private speaker print")
            state, item, prepared = dict(voices=[], readOnly=False), None, False
        if self.closed:
            return False
        self.voices_loaded = True
        self.voice_state, self.voice_data = state, item
        self.voice_prepared = prepared
        old = (item or {}).get("match", {})
        voice = next((v for v in state["voices"] if v["id"] == old.get("voiceId")), None)
        if voice and name_key(voice["name"]) == name_key(self.initial_name) == name_key(self.entry.get_text()):
            self.chosen_voice = voice["id"]
        self._voice_rows()
        self._validate()
        if self._voices_again:
            self._voices_again = False
            self._load_voices()
        return False

    def refresh_voices(self):
        self._validate()
        self._load_voices()

    def _open_voice_settings(self, *_):
        app = self.window.get_application()
        self.close()
        app.on_preferences()
        app.settings_dialog.set_visible_page_name("transcription")

    def _voice_rows(self):
        self.voices_list.remove_all()
        rejected = set((self.voice_data or {}).get("rejected", []))
        rows = ranked(
            self.voice_data["embedding"] if self.voice_data else None,
            [v for v in self.voice_state["voices"] if v["id"] not in rejected],
        )
        shown = 0
        for value in rows:
            voice = value["voice"]
            if voice["id"] in rejected:
                continue
            row = Gtk.ListBoxRow(activatable=True, css_classes=["speaker-voice-row"])
            row._voice = voice
            box = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
            slot = saved_voice_slot(voice)
            avatar = Gtk.Label(
                label=speaker_edits.voice_initial(voice["name"]),
                valign=Gtk.Align.CENTER,
                css_classes=["speaker-voice-avatar", f"speaker-avatar-{slot}"],
            )
            avatar.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
            box.append(avatar)
            labels_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=1,
                hexpand=True,
                valign=Gtk.Align.CENTER,
            )
            labels_box.append(
                Gtk.Label(
                    label=voice["name"],
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    css_classes=["speaker-voice-name"],
                )
            )
            a11y_parts = [voice["name"]]
            if value["stale"]:
                stale_text = text("savedVoices.row.stale")
                labels_box.append(Gtk.Label(label=stale_text, xalign=0, wrap=True, css_classes=["speaker-voice-stale"]))
                a11y_parts.append(stale_text)
            box.append(labels_box)
            if value["likely"]:
                likely_text = text("speakers.savedVoices.likely")
                box.append(
                    Gtk.Label(
                        label=likely_text,
                        valign=Gtk.Align.CENTER,
                        css_classes=["speaker-likely-tag"],
                    )
                )
                a11y_parts.append(likely_text)
            row.set_child(box)
            row.update_property([Gtk.AccessibleProperty.LABEL], [", ".join(a11y_parts)])
            self.voices_list.append(row)
            shown += 1
        visible = shown > 0
        self.voices_scroll.set_min_content_height(min(138, shown * 46) if visible else -1)
        self.voices_section.set_visible(visible)
        self.voices_heading.set_visible(visible)
        self.voices_list.set_visible(visible)
        self.voices_scroll.set_visible(visible)

    def _voice_row_activated(self, _listbox, row):
        voice = getattr(row, "_voice", None)
        if voice is not None:
            self._pick_voice(voice)

    def _pick_voice(self, voice):
        if self.busy:
            return
        self.entry.set_text(voice["name"])
        self.chosen_voice = voice["id"]
        self._validate()

    def _answer_duplicate(self, same):
        if self.candidate is None:
            return
        if same:
            self.chosen_voice = self.candidate["id"]
            self.entry.set_text(self.candidate["name"])
            self.chosen_voice = self.candidate["id"]
        else:
            self.declined_duplicate = name_key(self.entry.get_text())
            self.chosen_voice = None
        self._validate()

    def _remember_changed(self, *_):
        self.retry = None
        self.save.set_label(text("speakers.saveName"))
        self.error.set_visible(False)
        self._validate()

    def _changed(self, *_):
        self.chosen_voice = None
        self.retry = None
        self.save.set_label(text("speakers.saveName"))
        self.error.set_visible(False)
        self._validate()

    def _validate(self, *_):
        try:
            speaker_edits.name_value(self.entry.get_text())
        except ValueError:
            valid = False
        else:
            valid = True
        # A queued or running transcription job may re-diarize this id and
        # restamp it (G12); remember() would then refuse the print, so wait.
        transcriber = getattr(self.window.get_application(), "transcriber", None)
        pending = transcriber is not None and transcriber.transcribing(self.meeting.directory)
        enough = bool(self.voice_data and self.voice_data["seconds"] >= 4 and self.identifier != "me" and not pending)
        writable = not self.voice_state["readOnly"]
        app = self.window.get_application()
        ready = all(
            model is not None and model.snapshot().get("ready") for model in (app.voice_models, app.diarization_models)
        )
        self.remember.set_sensitive(enough and writable and not self.busy)
        self.voices_list.set_sensitive(not self.busy)
        self.duplicate.set_sensitive(not self.busy)
        typed = self.entry.get_text().strip()
        remember_body = (
            text("speakers.rememberVoice.cardBody.computer", name=typed)
            if typed
            else text("speakers.rememberVoice.cardBody.computer.unnamed")
        )
        self.remember_description.set_label(
            text("savedVoices.newerVersion")
            if not writable
            else remember_body
            if enough
            else text("speakers.rememberVoice.modelsNeeded")
            if not ready
            else text("speakers.rememberVoice.notPrepared")
            if not self.voice_prepared or pending
            else text("speakers.rememberVoice.tooShort")
        )
        show_remember = self.identifier != "me"
        self.remember_card.set_visible(show_remember)
        self.remember.set_visible(show_remember)
        self.remember_description.set_visible(show_remember)
        self.voice_settings.set_visible(show_remember and writable and not enough and not ready)
        self.candidate = None
        if (
            enough
            and writable
            and self.remember.get_active()
            and self.chosen_voice is None
            and name_key(self.entry.get_text()) != self.declined_duplicate
        ):
            scores = {
                r["voice"]["id"]: r["score"]
                for r in ranked(self.voice_data["embedding"], self.voice_state["voices"])
                if r["score"] is not None
            }
            self.candidate = pick_name(self.entry.get_text(), self.voice_state["voices"], scores)
        self.duplicate.set_visible(self.candidate is not None)
        if self.candidate:
            self.duplicate_title.set_markup(speaker_edits.duplicate_notice_markup(self.candidate["name"]))
        self.save.set_sensitive(
            self.voices_loaded and not self.busy and (valid and self.candidate is None or self.retry is not None)
        )

    def _save(self, *_):
        if not self.voices_loaded or self.closed or self.busy:
            return
        if self.retry is not None:
            operation = self.retry
        else:
            value = self.entry.get_text()
            remember = self.remember.get_sensitive() and self.remember.get_active()
            voice_id = self.chosen_voice
            directory = self.meeting.directory
            app = self.window.get_application()

            def operation():
                result = speaker_edits.save_name(directory, self.identifier, value, scanner=self.window.monitor.scanner)
                if self.voice_data is not None and not self.voice_state["readOnly"]:
                    try:
                        app.voice_store.remember(
                            directory, self.identifier, value, voice_id=voice_id, remember=remember
                        )
                    except (OSError, ValueError) as error:
                        raise VoiceWriteError(str(error)) from error
                return result

        self._submit(operation)

    def _merge(self, target):
        self._listening_merge = self.page.prepare_speaker_merge(self.identifier, target)
        directory = self.meeting.directory

        def operation():
            result = speaker_edits.merge_into(directory, self.identifier, target, scanner=self.window.monitor.scanner)
            self._recompute = (self.identifier, target)
            return result

        self._submit(operation)

    def _submit(self, operation):
        if self.closed or self.busy or self.window.monitor._closed:
            return
        self._finish_sample()
        self.busy = True
        self.error.set_visible(False)
        self.save.set_sensitive(False)
        self.merge.set_sensitive(False)
        self.entry.set_sensitive(False)
        self._validate()
        directory = self.meeting.directory
        meeting_key = self.meeting.key
        app = self.window.get_application()

        def write():
            if self.closed:
                raise CancelledError()
            return operation()

        self.future = self.window.monitor._executor.submit(write)
        self.future.add_done_callback(
            lambda future: GLib.idle_add(self._finished, future, operation, directory, meeting_key, app)
        )

    def _finished(self, future, operation, directory, meeting_key, app):
        self.busy = False
        try:
            future.result()
        except CancelledError:
            return False
        except (OSError, ValueError) as error:
            LOG.exception("Speaker edit failed; retaining entered name")
            if not self.closed:
                self.retry = operation
                self.error.set_label(
                    text("savedVoices.changeFailed" if isinstance(error, VoiceWriteError) else "speakers.changeFailed")
                )
                self.error.set_visible(True)
                if isinstance(error, VoiceWriteError):
                    self.window.monitor.refresh_now()
                self.save.set_label(text("common.tryAgain"))
                self.entry.set_sensitive(True)
                self.update(self.meeting)
                self._validate()
        else:
            if getattr(self, "_recompute", None):
                if (
                    not self.closed
                    and not self.page._closed
                    and self.page.meeting.key == meeting_key
                    and self.page.meeting.directory == directory
                ):
                    self.page.speaker_merged(*self._recompute, transition=self._listening_merge)
                app.recompute_voices(directory, self._recompute)
            if not self.window.monitor._closed:
                self.window.monitor.refresh_now()
            self.close()
        return False

    def _can_play(self):
        return bool(
            self.meeting.has_audio
            and self.page.player.loaded
            and self.page.player._prerolled
            and self.window._loaded_key == self.meeting.key
            and not self.window._playback_failed
        )

    def _play(self, *_):
        if self.sample_active:
            self._finish_sample()
            return
        if self.sample and self._can_play():
            self._sample_position = self.page.player.position
            self.sample_active = self.page.player.play_range(*self.sample)
            self._tick()

    def _finish_sample(self):
        if self.sample_active:
            if self._can_play():
                self.page.player.pause()
                self.page.player.clear_range()
                self.page.player.seek(self._sample_position)
            else:
                self.page.player.clear_range()
            self.sample_active = False

    def _tick(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.sample_active and not self.page.player.playing:
            self._finish_sample()
        self.play.set_sensitive(not self.busy and self.sample is not None and self._can_play())
        self.play.set_icon_name(
            "media-playback-stop-symbolic" if self.sample_active else "media-playback-start-symbolic"
        )
        self.play.update_property(
            [Gtk.AccessibleProperty.LABEL], [text("common.stop" if self.sample_active else "speakers.playSample")]
        )
        return GLib.SOURCE_CONTINUE

    def _closed(self, *_):
        self.closed = True
        self.window._editor_focus(self.entry, False)
        self._finish_sample()
        if self.future is not None:
            self.future.cancel()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
