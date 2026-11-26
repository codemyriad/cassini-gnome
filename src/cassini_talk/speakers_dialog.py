# SPDX-License-Identifier: GPL-3.0-or-later
"""UX16 speaker naming and merge dialog; metadata IO uses the Library worker.

D112 (L-86): every speaker sits in a row at the top; each keeps its own form
(name, saved voice, Remember this voice, Background voice) while you move
between them, and one Save applies every change.
"""

import logging
import re
from concurrent.futures import CancelledError
from dataclasses import dataclass

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import speaker_edits, ux_formats
from .adw_compat import wrap_box
from .strings import text
from .tokens import saved_voice_slot, speaker_slot
from .voices_logic import name_key, pick_name, ranked

LOG = logging.getLogger(__name__)


class VoiceWriteError(OSError):
    pass


@dataclass
class _Form:
    """One speaker's pending changes, kept while the dialog moves between speakers."""

    identifier: str
    initial_name: str
    name: str
    initial_background: bool
    background: bool
    remember: bool
    chosen_voice: str | None = None
    declined_duplicate: str | None = None
    # A duplicate-voice question was left unanswered on this speaker.
    blocked: bool = False
    visited: bool = False
    initial_remember: bool | None = None

    @property
    def remember_changed(self):
        return self.initial_remember is not None and self.remember != self.initial_remember

    @property
    def name_changed(self):
        return self.name.strip() != self.initial_name.strip()


class _WidthHint(Gtk.Widget):
    """Zero-height child asking for a natural width without raising the minimum.

    The dialog follows its content's size (so it grows when the duplicate notice
    or saved voices appear), which made the footer buttons set its width: the
    shorter D112 "Save" label shrank the sheet until the wrapped body no longer
    fit a 710 px window. This keeps the designed width, still narrowing on a
    phone-width window.
    """

    def __init__(self, width):
        super().__init__(accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self._width = width

    def do_measure(self, orientation, _for_size):
        natural = self._width if orientation == Gtk.Orientation.HORIZONTAL else 0
        return 0, natural, -1, -1


class SpeakersDialog(Adw.Dialog):
    def __init__(self, page, identifier):
        super().__init__(
            title=text("speakers.dialog.a11y"),
            content_width=540,
            follows_content_size=True,
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
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=20,
            margin_bottom=14,
            margin_start=24,
            margin_end=24,
        )
        self.body_scroll = Gtk.ScrolledWindow(
            child=content,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            vexpand=True,
        )
        outer.append(self.body_scroll)
        self.forms = {}
        self.chips = {}
        self._chip_key = None
        self._loading_form = False
        self.voice_items = {}
        narrow = 0 < self.window.get_width() < 420
        # D112 (2C): every speaker across the top, in the order of the names under the title.
        # Wide, the row wraps; narrow, it scrolls sideways (D-1162 bottom sheet).
        if narrow:
            self.chip_box = Gtk.Box(spacing=6)
            chip_holder = Gtk.ScrolledWindow(
                child=self.chip_box,
                hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                vscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
            )
        else:
            self.chip_box = chip_holder = wrap_box(child_spacing=6, line_spacing=4)
        self.chip_box.update_property([Gtk.AccessibleProperty.LABEL], [text("common.speakers")])
        self.speaker_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["speaker-chip-row"])
        self.speaker_row.append(chip_holder)
        content.append(self.speaker_row)
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
        self.duplicate = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=False, css_classes=["speaker-duplicate"]
        )
        self.duplicate_row = Gtk.Box(spacing=8)
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
        # D-1162: while Save is dimmed by this choice, say why under its line.
        self.duplicate_hint = Gtk.Label(
            label=text("speakers.duplicate.pickHint"),
            xalign=0,
            wrap=True,
            visible=False,
            css_classes=["dim-label", "caption", "speaker-duplicate-hint"],
        )
        self.duplicate_row.append(self.duplicate_title)
        self.duplicate_buttons = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.duplicate_buttons.append(self.someone_else)
        self.duplicate_buttons.append(self.same_person)
        self.duplicate_row.append(self.duplicate_buttons)
        self.duplicate.append(self.duplicate_row)
        self.duplicate.append(self.duplicate_hint)
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
        # D103 §3: the default follows the voices question / "Remember people I name".
        self._remember_default = self.window.get_application().recording_settings.get_boolean("remember-voices")
        self.remember = Gtk.Switch(
            active=self._remember_default,
            sensitive=False,
            valign=Gtk.Align.START,
            css_classes=["speaker-remember-switch"],
        )
        self.remember.update_property([Gtk.AccessibleProperty.LABEL], [text("speakers.rememberVoice.title")])
        self.remember.connect("notify::active", self._remember_changed)
        self.remember_card.append(remember_copy)
        self.remember_card.append(self.remember)
        content.append(self.remember_card)
        # D112 (L-86): styled like Remember this voice.
        self.background_card = Gtk.Box(spacing=16, css_classes=["speaker-remember-card", "speaker-background-card"])
        background_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        background_copy.append(
            Gtk.Label(
                label=text("speakers.backgroundVoice"),
                xalign=0,
                wrap=True,
                css_classes=["speaker-remember-title"],
            )
        )
        background_copy.append(
            Gtk.Label(
                label=text("speakers.backgroundVoice.description"),
                xalign=0,
                wrap=True,
                css_classes=["speaker-remember-body"],
            )
        )
        self.background = Gtk.Switch(valign=Gtk.Align.START, css_classes=["speaker-remember-switch"])
        self.background.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [text("speakers.backgroundVoice"), text("speakers.backgroundVoice.description")],
        )
        self.background.connect("notify::active", self._background_changed)
        self.background_card.append(background_copy)
        self.background_card.append(self.background)
        content.append(self.background_card)
        self.error = Gtk.Label(
            label=text("speakers.changeFailed"),
            xalign=0,
            wrap=True,
            visible=False,
            css_classes=["error", "error-line"],
        )
        content.append(self.error)
        # D-1162: wide, one row (Merge into… · Cancel · Save); narrow, the buttons stack: Save across
        # the width, then Cancel and Merge into… side by side, so none is cut (_set_narrow).
        self.footer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=2,
            margin_bottom=20,
            margin_start=24,
            margin_end=24,
        )
        footer = self.footer_row = Gtk.Box(spacing=8)
        self.footer.append(footer)
        self.merge_menu = Gio.Menu()
        self.merge = Gtk.MenuButton(
            label=text("speakers.mergeInto"),
            can_shrink=True,
            menu_model=self.merge_menu,
            css_classes=["speaker-btn-secondary"],
        )
        footer.append(self.merge)
        self.footer_spacer = Gtk.Box(hexpand=True)
        footer.append(self.footer_spacer)
        self.cancel = Gtk.Button(
            label=text("common.cancel"),
            can_shrink=True,
            css_classes=["speaker-btn-secondary"],
        )
        self.cancel.connect("clicked", lambda *_: self.close())
        self.save = Gtk.Button(
            label=text("speakers.saveName"),
            can_shrink=True,
            css_classes=["suggested-action", "speaker-btn-primary"],
        )
        self.save.connect("clicked", self._save)
        footer.append(self.cancel)
        footer.append(self.save)
        outer.append(self.footer)
        outer.append(_WidthHint(540))
        self.set_child(outer)
        self.narrow = False
        # D-1162: on a phone-width window (where the dialog is a bottom sheet) the buttons stack.
        self._set_narrow(0 < self.window.get_width() < 420)
        self.actions = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new("merge", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, target: self._merge(target.get_string()))
        self.actions.add_action(action)
        self.insert_action_group("speaker", self.actions)
        self.update(self.meeting)
        form = self._form(identifier)
        form.visited = True
        self.initial_name = form.initial_name
        self.entry.set_text("" if form.background else form.name)
        self._loading_form = True
        self.background.set_active(form.background)
        self._loading_form = False
        self._apply_background()
        self.entry.connect("changed", self._changed)
        self.entry.connect("map", lambda *_: GLib.idle_add(self._place_caret))
        self._validate()
        self.set_default_widget(self.save)
        self.timer = GLib.timeout_add(50, self._tick)
        self._load_voices()

    def _stored_name(self, speaker):
        # Generated Speaker n is a display placeholder, not a stored name.
        raw = next((s for s in self.meeting.metadata.get("speakers", []) if s.get("id") == speaker.id), {})
        value = raw.get("name")
        if value is None and speaker.provenance != "unnamed" and not re.fullmatch(r"Speaker \d+", speaker.name):
            value = speaker.name
        return value or ""

    def _form(self, identifier):
        form = self.forms.get(identifier)
        if form is None:
            speaker = next(s for s in self.meeting.speakers if s.id == identifier)
            name = self._stored_name(speaker)
            background = bool(getattr(speaker, "background", False))
            form = _Form(identifier, name, name, background, background, self._remember_default)
            form.initial_remember = form.remember
            self.forms[identifier] = form
            if self.voices_loaded:
                old = (self.voice_items.get(identifier) or {}).get("match", {})
                voice = next((v for v in self.voice_state["voices"] if v["id"] == old.get("voiceId")), None)
                if voice and name and name_key(voice["name"]) == name_key(name):
                    form.chosen_voice = voice["id"]
        return form

    def _keep_form(self):
        """Copy the shown speaker's widgets into its form."""
        form = self.forms.get(self.identifier)
        if form is None:
            return
        if not form.background:
            form.name = self.entry.get_text()
        form.background = self.background.get_active()
        form.remember = self.remember.get_active()
        form.chosen_voice = self.chosen_voice
        form.declined_duplicate = self.declined_duplicate
        form.blocked = self.candidate is not None and not form.background

    def select_speaker(self, identifier):
        """D112 (2C): switch the form to another speaker without closing."""
        if identifier == self.identifier or self.busy or self.closed:
            self._sync_chips()
            return
        if not any(s.id == identifier for s in self.meeting.speakers):
            return
        self._finish_sample()
        self._keep_form()
        self.identifier = identifier
        self.page._selected_speaker = identifier
        form = self._form(identifier)
        form.visited = True
        self.initial_name = form.initial_name
        self.retry = None
        self.error.set_visible(False)
        self.save.set_label(text("speakers.saveName"))
        self._loading_form = True
        self.chosen_voice = form.chosen_voice
        self.declined_duplicate = form.declined_duplicate
        self.remember.set_active(form.remember)
        self.background.set_active(form.background)
        self.entry.set_text("" if form.background else form.name)
        self._loading_form = False
        self._content_key = None
        self.update(self.meeting)
        self.voice_data = self.voice_items.get(identifier)
        self._apply_background()
        self._voice_rows()
        self._validate()
        self._sync_chips()

    def _display_name(self, speaker):
        form = self.forms.get(speaker.id)
        if form is not None and not form.background and form.name.strip():
            return form.name.strip()
        return speaker.name

    def _chip(self, speaker):
        dot = Gtk.Box(valign=Gtk.Align.CENTER, css_classes=["speaker-dot"])
        dot.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        name = Gtk.Label(
            xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=18, css_classes=["speaker-chip-name"]
        )
        content = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        content.append(dot)
        content.append(name)
        chip = Gtk.ToggleButton(child=content, css_classes=["flat", "speaker-chip"], can_shrink=True)
        chip._dot, chip._name, chip._id = dot, name, speaker.id
        chip.connect("toggled", self._chip_toggled)
        return chip

    def _chip_toggled(self, chip):
        if self._loading_form:
            return
        if chip.get_active():
            self.select_speaker(chip._id)
        elif chip._id == self.identifier:
            # Clicking the shown speaker keeps it shown.
            self._sync_chips()

    def _sync_chips(self):
        """Rebuild the speaker row when the roster changes; restyle chips for pending changes."""
        speakers = self.meeting.speakers
        key = tuple(s.id for s in speakers)
        if key != self._chip_key:
            self._chip_key = key
            while child := self.chip_box.get_first_child():
                self.chip_box.remove(child)
            self.chips = {}
            for speaker in speakers:
                chip = self._chip(speaker)
                self.chips[speaker.id] = chip
                self.chip_box.append(chip)
        self.speaker_row.set_visible(len(speakers) > 1)
        self._loading_form = True
        for speaker in speakers:
            chip = self.chips[speaker.id]
            form = self.forms.get(speaker.id)
            background = form.background if form is not None else bool(getattr(speaker, "background", False))
            shown = self._display_name(speaker)
            slot = speaker_slot(speaker)
            for index in range(7):
                (chip._dot.add_css_class if index == slot else chip._dot.remove_css_class)(f"speaker-dot-{index}")
            (chip._dot.add_css_class if background else chip._dot.remove_css_class)("speaker-dot-hollow")
            (chip._name.add_css_class if background else chip._name.remove_css_class)("speaker-chip-background")
            unnamed = slot == 0 and not background and shown == speaker.name
            (chip._name.add_css_class if unnamed else chip._name.remove_css_class)("speaker-unnamed")
            chip._name.set_label(shown)
            label = text("speakers.backgroundVoice") if background else ""
            chip.update_property([Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION], [shown, label])
            chip.set_active(speaker.id == self.identifier)
            chip.set_sensitive(not self.busy)
        self._loading_form = False

    def _apply_background(self):
        """Background voice on: the name, saved voices and Merge into… step back; Remember is hidden."""
        on = self.background.get_active()
        self.entry.set_sensitive(not on and not self.busy)
        self.entry.set_placeholder_text(text("speakers.backgroundVoice.namePlaceholder") if on else "")
        self.voices_section.set_sensitive(not on)
        self.merge.set_sensitive(not on and not self.busy and len(self.meeting.speakers) > 1)

    def _background_changed(self, *_):
        if self._loading_form:
            return
        form = self.forms.get(self.identifier)
        on = self.background.get_active()
        if form is not None:
            if on:
                form.name = self.entry.get_text()
            form.background = on
        self._loading_form = True
        # The stored name is kept; the field shows "Not needed for a background voice" while on.
        self.entry.set_text("" if on else (form.name if form is not None else self.entry.get_text()))
        self._loading_form = False
        self.retry = None
        self.save.set_label(text("speakers.saveName"))
        self.error.set_visible(False)
        self._apply_background()
        self._validate()
        self._sync_chips()

    def _set_narrow(self, narrow):
        """D-1162: stack the footer and the Same person / Someone else choice on a narrow window."""
        if narrow == self.narrow:
            return
        self.narrow = narrow
        for button in (self.merge, self.cancel, self.save):
            button.get_parent().remove(button)
        if narrow:
            self.footer.prepend(self.save)
            self.footer_row.set_homogeneous(True)
            self.footer_spacer.set_visible(False)
            self.footer_row.append(self.cancel)
            self.footer_row.append(self.merge)
        else:
            self.footer_row.set_homogeneous(False)
            self.footer_spacer.set_visible(True)
            self.footer_row.prepend(self.merge)
            self.footer_row.append(self.cancel)
            self.footer_row.append(self.save)
        self.duplicate_row.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self.duplicate_buttons.set_homogeneous(narrow)
        self.duplicate_buttons.set_halign(Gtk.Align.FILL if narrow else Gtk.Align.END)

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
        # The Library republishes the meeting every activity tick; only rebuild
        # the quote and merge menu when speakers, merges or transcript changed.
        key = (
            self.identifier,
            meeting.speakers,
            meeting.metadata.get("speakerMerges"),
            meeting.listening_turns,
            meeting.turns,
            meeting.words,
        )
        previous = getattr(self, "_content_key", None)
        if previous is None or not (
            key[:3] == previous[:3] and all(a is b for a, b in zip(key[3:], previous[3:], strict=True))
        ):
            self._content_key = key
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
        background = self.background.get_active()
        self.merge.set_sensitive(not self.busy and not background and len(meeting.speakers) > 1)
        self._sync_chips()

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
            return state, dict(cache["speakers"]), prepared

        future = self.window.monitor._executor.submit(load)
        future.add_done_callback(lambda f: GLib.idle_add(self._voices_loaded, f))

    def _voices_loaded(self, future):
        self._voices_loading = False
        try:
            state, items, prepared = future.result()
        except CancelledError:
            return False
        except (OSError, ValueError):
            LOG.exception("Could not load private speaker print")
            state, items, prepared = dict(voices=[], readOnly=False), None, False
        if self.closed:
            return False
        self.voices_loaded = True
        self.voice_items = items or {}
        item = self.voice_items.get(self.identifier)
        self.voice_state, self.voice_data = state, item
        self.voice_prepared = prepared
        for form in self.forms.values():
            # A speaker already confirmed as a saved voice keeps that voice while its name is unchanged.
            old = (self.voice_items.get(form.identifier) or {}).get("match", {})
            voice = next((v for v in state["voices"] if v["id"] == old.get("voiceId")), None)
            current = self.entry.get_text() if form.identifier == self.identifier else form.name
            if voice and name_key(voice["name"]) == name_key(form.initial_name) == name_key(current):
                form.chosen_voice = voice["id"]
                if form.identifier == self.identifier:
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
        if self._loading_form:
            return
        self.retry = None
        self.save.set_label(text("speakers.saveName"))
        self.error.set_visible(False)
        self._validate()

    def _changed(self, *_):
        if self._loading_form:
            return
        self.chosen_voice = None
        self.retry = None
        self.save.set_label(text("speakers.saveName"))
        self.error.set_visible(False)
        form = self.forms.get(self.identifier)
        if form is not None and not form.background:
            form.name = self.entry.get_text()
        self._validate()
        self._sync_chips()

    def _enough(self, identifier, item, pending):
        return bool(item and item["seconds"] >= 4 and identifier != "me" and not pending)

    def _others_ready(self):
        """Every other speaker's pending form can be saved."""
        for form in self.forms.values():
            if form.identifier == self.identifier or form.background or not form.name_changed:
                continue
            if form.blocked:
                return False
            try:
                speaker_edits.name_value(form.name)
            except ValueError:
                return False
        return True

    def _validate(self, *_):
        background = self.background.get_active()
        try:
            speaker_edits.name_value(self.entry.get_text())
        except ValueError:
            valid = background
        else:
            valid = True
        valid = valid and self._others_ready()
        # A queued or running transcription job may re-diarize this id and
        # restamp it (G12); remember() would then refuse the print, so wait.
        transcriber = getattr(self.window.get_application(), "transcriber", None)
        pending = transcriber is not None and transcriber.transcribing(self.meeting.directory)
        enough = self._enough(self.identifier, self.voice_data, pending)
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
        # D112: no voiceprint is kept for a background voice, so Remember this voice is hidden.
        show_remember = self.identifier != "me" and not background
        self.remember_card.set_visible(show_remember)
        self.remember.set_visible(show_remember)
        self.remember_description.set_visible(show_remember)
        self.voice_settings.set_visible(show_remember and writable and not enough and not ready)
        self.candidate = None
        if (
            enough
            and writable
            and not background
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
        self.duplicate_hint.set_visible(self.candidate is not None)
        if self.candidate:
            self.duplicate_title.set_markup(speaker_edits.duplicate_notice_markup(self.candidate["name"]))
        self.save.set_sensitive(
            self.voices_loaded and not self.busy and (valid and self.candidate is None or self.retry is not None)
        )

    def _changes(self):
        """The pending speaker changes for one Save, plus their forms (D112)."""
        self._keep_form()
        changes, forms = {}, []
        visible = {s.id for s in self.meeting.speakers}
        for form in self.forms.values():
            if form.identifier not in visible:
                continue
            change = {}
            if form.background != form.initial_background:
                change["background"] = form.background
            # The shown speaker is saved as before (a Save confirms its name or saved voice);
            # others only when their name, chosen voice or Remember changed.
            if not form.background and (
                form.name_changed
                or form.chosen_voice is not None
                or form.remember_changed
                or form.identifier == self.identifier
            ):
                change["name"] = form.name
            if change:
                changes[form.identifier] = change
                forms.append(form)
        return changes, forms

    def _save(self, *_):
        if not self.voices_loaded or self.closed or self.busy:
            return
        if self.retry is not None:
            operation = self.retry
        else:
            changes, forms = self._changes()
            if not changes:
                self.close()
                return
            directory = self.meeting.directory
            app = self.window.get_application()
            scanner = self.window.monitor.scanner
            transcriber = getattr(app, "transcriber", None)
            pending = transcriber is not None and transcriber.transcribing(directory)
            writable = not self.voice_state["readOnly"]
            voices = []
            for form in forms:
                item = self.voice_items.get(form.identifier)
                if "name" in changes[form.identifier] and item is not None and writable:
                    remember = form.remember and self._enough(form.identifier, item, pending)
                    voices.append((form.identifier, form.name, form.chosen_voice, remember))
            names = {s.id: s.name for s in self.meeting.speakers}
            summary = speaker_edits.save_summary(
                [(f.identifier, f.initial_background, f.background, f.name if f.name_changed else None) for f in forms],
                names,
            )
            # A retry after a saved-voices failure must not replace the first write's Undo snapshot.
            outcome = {}

            def operation():
                result, previous = speaker_edits.save_speakers(directory, changes, scanner=scanner)
                outcome.setdefault("previous", previous)
                outcome["summary"] = summary
                for identifier, value, voice_id, remember in voices:
                    try:
                        app.voice_store.remember(directory, identifier, value, voice_id=voice_id, remember=remember)
                    except (OSError, ValueError) as error:
                        raise VoiceWriteError(str(error)) from error
                return outcome

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
        self._sync_chips()
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
            outcome = future.result()
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
                self._apply_background()
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
            elif isinstance(outcome, dict) and outcome.get("previous"):
                saved = getattr(self.page, "speakers_saved", None)
                if saved is not None:
                    saved(directory, meeting_key, outcome["previous"], outcome.get("summary"))
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
