# SPDX-License-Identifier: GPL-3.0-or-later
"""UX16 speaker naming and merge dialog; metadata IO uses the Library worker."""

import logging
import re
from concurrent.futures import CancelledError

from gi.repository import Adw, Gio, GLib, Gtk

from . import speaker_edits, ux_formats
from .meeting_text import COLOURS, DARK_COLOURS
from .strings import text
from .voices_logic import name_key, pick_name, ranked

LOG = logging.getLogger(__name__)


class VoiceWriteError(OSError):
    pass


class SpeakersDialog(Adw.Dialog):
    def __init__(self, page, identifier):
        super().__init__(title=text("common.speakers"), content_width=520, css_classes=["speakers-dialog"])
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
            spacing=8,
            margin_top=20,
            margin_bottom=16,
            margin_start=24,
            margin_end=24,
        )
        self.set_child(
            Gtk.ScrolledWindow(
                child=content,
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
                max_content_height=660,
            )
        )
        header = Gtk.Box(spacing=12)
        self.avatar = Gtk.Label(css_classes=["speaker-avatar"])
        header.append(self.avatar)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.heading = Gtk.Label(xalign=0, wrap=True, css_classes=["title-2"])
        self.talk = Gtk.Label(xalign=0, wrap=True, css_classes=["small", "dim-label"])
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
        field = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        name = Gtk.Label(label=text("speakers.nameLabel"), xalign=0, css_classes=["small", "heading"])
        self.entry = Gtk.Entry(hexpand=True, activates_default=True)
        self.window.protect_text_input(self.entry)
        name.set_mnemonic_widget(self.entry)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.nameLabel")])
        field.append(name)
        field.append(self.entry)
        content.append(field)
        self.voices_heading = Gtk.Label(
            label=text("speakers.savedVoices.heading"), xalign=0, css_classes=["small", "heading"], visible=False
        )
        content.append(self.voices_heading)
        self.voices_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"], visible=False)
        self.voices_scroll = Gtk.ScrolledWindow(
            child=self.voices_list,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            max_content_height=160,
            visible=False,
        )
        content.append(self.voices_scroll)
        self.duplicate = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False, css_classes=["speaker-duplicate"]
        )
        self.duplicate_title = Gtk.Label(xalign=0, wrap=True, css_classes=["heading"])
        self.duplicate.append(self.duplicate_title)
        self.duplicate.append(Gtk.Label(label=text("speakers.duplicate.question"), xalign=0))
        answers = Gtk.Box(spacing=8)
        self.same_person = Gtk.Button(label=text("speakers.duplicate.samePerson"))
        self.someone_else = Gtk.Button(label=text("speakers.duplicate.someoneElse"))
        self.same_person.connect("clicked", lambda *_: self._answer_duplicate(True))
        self.someone_else.connect("clicked", lambda *_: self._answer_duplicate(False))
        answers.append(self.same_person)
        answers.append(self.someone_else)
        self.duplicate.append(answers)
        content.append(self.duplicate)
        self.remember = Gtk.CheckButton(label=text("speakers.rememberVoice"), active=True, sensitive=False)
        self.remember.connect("toggled", self._remember_changed)
        content.append(self.remember)
        self.remember_description = Gtk.Label(xalign=0, wrap=True, css_classes=["caption", "dim-label"])
        content.append(self.remember_description)
        self.voice_settings = Gtk.Button(
            label=text("speakers.rememberVoice.settings"),
            halign=Gtk.Align.START,
            css_classes=["flat", "accent"],
            visible=False,
        )
        self.voice_settings.connect("clicked", self._open_voice_settings)
        content.append(self.voice_settings)
        self.error = Gtk.Label(
            label=text("speakers.changeFailed"), xalign=0, wrap=True, visible=False, css_classes=["warning"]
        )
        content.append(self.error)
        footer = Gtk.Box(spacing=8)
        self.merge_menu = Gio.Menu()
        self.merge = Gtk.MenuButton(
            label=text("speakers.mergeInto"), menu_model=self.merge_menu, css_classes=["flat", "accent"]
        )
        footer.append(self.merge)
        footer.append(Gtk.Box(hexpand=True))
        self.cancel = Gtk.Button(label=text("common.cancel"))
        self.cancel.connect("clicked", lambda *_: self.close())
        self.save = Gtk.Button(label=text("speakers.saveName"), css_classes=["suggested-action"])
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
        self._validate()
        self.set_default_widget(self.save)
        self.timer = GLib.timeout_add(50, self._tick)
        self._load_voices()

    def update(self, meeting):
        self.meeting = meeting
        speaker = next((s for s in meeting.speakers if s.id == self.identifier), None)
        if speaker is None or not meeting.metadata_editable:
            self.close()
            return
        self.heading.set_label(speaker.name)
        self.talk.set_label(text("speakers.talkTime", talkTime=ux_formats.talk_time(speaker.talk_ms)))
        palette = DARK_COLOURS if Adw.StyleManager.get_default().get_dark() else COLOURS
        colour = palette[speaker.colour_index % len(palette)]
        self.avatar.set_markup(f'<span foreground="{colour}">{speaker.colour_index + 1}</span>')
        self.sample = speaker.sample_span
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
        if state["voices"]:
            self.set_content_height(660)
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
        for value in rows:
            voice = value["voice"]
            if voice["id"] in rejected:
                continue
            row = Adw.ActionRow(title=voice["name"], use_markup=False, activatable=True)
            if value["stale"]:
                row.set_subtitle(text("savedVoices.row.stale"))
            if value["likely"]:
                row.add_suffix(Gtk.Label(label=text("speakers.savedVoices.likely"), css_classes=["success", "caption"]))
            row.connect("activated", lambda _row, v=voice: self._pick_voice(v))
            self.voices_list.append(row)
        visible = bool(rows)
        self.voices_heading.set_visible(visible)
        self.voices_list.set_visible(visible)
        self.voices_scroll.set_visible(visible)

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
        enough = bool(self.voice_data and self.voice_data["seconds"] >= 4 and self.identifier != "me")
        writable = not self.voice_state["readOnly"]
        app = self.window.get_application()
        ready = all(
            model is not None and model.snapshot().get("ready") for model in (app.voice_models, app.diarization_models)
        )
        self.remember.set_sensitive(enough and writable and not self.busy)
        self.voices_list.set_sensitive(not self.busy)
        self.duplicate.set_sensitive(not self.busy)
        self.remember_description.set_label(
            text("savedVoices.newerVersion")
            if not writable
            else text("speakers.rememberVoice.description", name=self.entry.get_text().strip())
            if enough
            else text("speakers.rememberVoice.modelsNeeded")
            if not ready
            else text("speakers.rememberVoice.notPrepared")
            if not self.voice_prepared
            else text("speakers.rememberVoice.tooShort")
        )
        self.voice_settings.set_visible(writable and not enough and not ready)
        self.remember.set_visible(self.identifier != "me")
        self.remember_description.set_visible(self.identifier != "me")
        self.voice_settings.set_visible(self.voice_settings.get_visible() and self.identifier != "me")
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
            self.duplicate_title.set_label(text("speakers.duplicate.title", name=self.candidate["name"]))
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

            def operation():
                result = speaker_edits.save_name(
                    self.meeting.directory, self.identifier, value, scanner=self.window.monitor.scanner
                )
                if self.voice_data is not None and not self.voice_state["readOnly"]:
                    try:
                        self.window.get_application().voice_store.remember(
                            self.meeting.directory, self.identifier, value, voice_id=voice_id, remember=remember
                        )
                    except (OSError, ValueError) as error:
                        raise VoiceWriteError(str(error)) from error
                return result

        self._submit(operation)

    def _merge(self, target):
        def operation():
            result = speaker_edits.merge_into(
                self.meeting.directory, self.identifier, target, scanner=self.window.monitor.scanner
            )
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

        def write():
            if self.closed:
                raise CancelledError()
            return operation()

        self.future = self.window.monitor._executor.submit(write)
        self.future.add_done_callback(lambda future: GLib.idle_add(self._finished, future, operation))

    def _finished(self, future, operation):
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
                self.window.get_application().recompute_voices(self.meeting.directory, self._recompute)
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
            "media-playback-pause-symbolic" if self.sample_active else "media-playback-start-symbolic"
        )
        self.play.update_property(
            [Gtk.AccessibleProperty.LABEL], [text("player.pause" if self.sample_active else "speakers.playSample")]
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
