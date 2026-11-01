# window.py
#
# Copyright 2026 Silvio Tomatis
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import threading
from datetime import date

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from . import calendar_formats, portable, summary, transcript
from . import ux_formats as fmt
from .colors import SpeakerPalette, hex_color
from .i18n import gettext as _
from .i18n import ngettext, number
from .player import Player
from .portable import State
from .strings import text
from .transcript import format_time

RESOURCE = "/io/codemyriad/CassiniTalk"
SKIP_MS = 15_000
NUDGE_MS = 5_000


class TurnItem(GObject.Object):
    __gtype_name__ = "CassiniTurnItem"

    def __init__(self, turn):
        super().__init__()
        self.turn = turn


class TurnRow(Gtk.Box):
    """One paragraph of the transcript: speaker, start time, clickable words."""

    __gtype_name__ = "CassiniTurnRow"

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["turn"])
        self.window = window
        self.turn = None
        self.offsets: list[int] = []

        header = Gtk.Box(spacing=8)
        self.dot = Gtk.Box(css_classes=["speaker-dot"], valign=Gtk.Align.CENTER)
        self.dot.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        self.name = Gtk.Label(xalign=0, css_classes=["heading"], ellipsize=Pango.EllipsizeMode.END)
        self.time = Gtk.Button(css_classes=["flat", "timestamp", "numeric"], valign=Gtk.Align.CENTER)
        self.time.connect("clicked", self._on_time_clicked)
        header.append(self.dot)
        header.append(self.name)
        header.append(self.time)
        self.over = Gtk.Label(xalign=0, css_classes=["dimmed", "caption"], ellipsize=Pango.EllipsizeMode.END)
        header.append(self.over)

        self.words = Gtk.Label(
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            use_markup=True,
            css_classes=["words"],
        )
        self.words.connect("activate-link", self._on_activate_link)
        self.append(header)
        self.append(self.words)

    def bind(self, turn):
        self.turn = turn
        palette = self.window.palette
        for cls in list(self.dot.get_css_classes()):
            if cls.startswith("speaker-") and cls != "speaker-dot":
                self.dot.remove_css_class(cls)
        self.dot.add_css_class(palette.css_class(turn.speaker))
        self.name.set_label(self.window.speaker_label(turn.speaker))
        self.time.set_label(format_time(turn.start_ms))
        self.time.set_tooltip_text(_("Play from {time}").format(time=format_time(turn.start_ms)))
        names = ", ".join(self.window.speaker_label(speaker) for speaker in turn.over)
        self.over.set_label(_("(over {names})").format(names=names) if names else "")
        self.over.set_tooltip_text(_("Simultaneous speech"))
        self.render()

    def render(self):
        turn = self.turn
        if turn is None:
            return
        markup, self.offsets = self.window.turn_markup(turn)
        self.words.set_markup(markup)
        if turn.index in self.window.active_turns:
            self.add_css_class("current")
        else:
            self.remove_css_class("current")

    def _on_time_clicked(self, _button):
        if self.turn is not None:
            self.window.seek_and_play(self.turn.start_ms + 1)

    def _on_activate_link(self, _label, uri):
        # Seeking synchronously changes the highlight markup, which destroys
        # GtkLabel's link controllers while GTK is still dispatching this click.
        # Keep the URI (not the recycled row's turn) and wait for dispatch to end.
        GLib.idle_add(self._activate_link_later, uri, self.window._generation)
        return True

    def _activate_link_later(self, uri, generation):
        if generation != self.window._generation or not self.window.get_visible():
            return GLib.SOURCE_REMOVE
        kind, _sep, value = uri.partition(":")
        if kind == "w":
            turn_index, _sep, word = value.partition("/")
            self.window.seek_to_word(int(turn_index), int(word))
        elif kind == "t":
            self.window.seek_and_play(self.window.turns[int(value)].start_ms + 1)
        return GLib.SOURCE_REMOVE


class SpeakerRow(Gtk.ListBoxRow):
    __gtype_name__ = "CassiniSpeakerRow"

    def __init__(self, speaker_id, label, css_class):
        super().__init__(activatable=False)
        self.speaker_id = speaker_id
        box = Gtk.Box(spacing=10)
        dot = Gtk.Box(css_classes=["speaker-dot", css_class], valign=Gtk.Align.CENTER)
        dot.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        box.append(dot)
        box.append(Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        self.speaking = Gtk.Image(icon_name="audio-volume-high-symbolic", css_classes=["accent"], opacity=0)
        self.speaking.set_tooltip_text(_("Speaking now"))
        box.append(self.speaking)
        self.set_child(box)

    def set_speaking(self, speaking):
        self.speaking.set_opacity(1 if speaking else 0)


@Gtk.Template(resource_path=f"{RESOURCE}/ui/window.ui")
class CassiniWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CassiniWindow"

    toast_overlay = Gtk.Template.Child()
    main_stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    inspector_split = Gtk.Template.Child()
    sidebar_split = Gtk.Template.Child()
    properties_box = Gtk.Template.Child()
    transcript_chooser = Gtk.Template.Child()
    transcript_dropdown = Gtk.Template.Child()
    speakers_heading = Gtk.Template.Child()
    speakers_list = Gtk.Template.Child()
    file_label = Gtk.Template.Child()
    title_stack = Gtk.Template.Child()
    window_title = Gtk.Template.Child()
    search_bar = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()
    match_label = Gtk.Template.Child()
    banner = Gtk.Template.Child()
    view_stack = Gtk.Template.Child()
    transcript_stack = Gtk.Template.Child()
    transcript_scroller = Gtk.Template.Child()
    transcript_list = Gtk.Template.Child()
    transcript_status = Gtk.Template.Child()
    summary_page = Gtk.Template.Child()
    summary_box = Gtk.Template.Child()
    elapsed_label = Gtk.Template.Child()
    remaining_label = Gtk.Template.Child()
    position_scale = Gtk.Template.Child()
    position_adjustment = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    rate_button = Gtk.Template.Child()
    switcher_bar = Gtk.Template.Child()
    narrow_breakpoint = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        app = self.get_application()
        self.settings = app.recording_settings
        self.empty_page.set_icon_name(app.get_application_id())
        self.empty_page.set_title(text("app.name"))
        self.window_title.set_title(text("app.name"))
        from .identity import product_menu

        def name_menus(widget):
            if isinstance(widget, Gtk.MenuButton) and widget.get_menu_model() is not None:
                widget.set_menu_model(product_menu(widget.get_menu_model()))
            child = widget.get_first_child()
            while child is not None:
                name_menus(child)
                child = child.get_next_sibling()

        name_menus(self)
        self.style_manager = Adw.StyleManager.get_default()

        self.player = Player()
        self.get_application().watch_player(self.player)
        self.palette = SpeakerPalette()
        self.file: Gio.File | None = None
        self.meeting: portable.Meeting | None = None
        self.audio_check: portable.AudioCheck | None = None
        self.transcript_id: str | None = None
        self.transcript_error: str | None = None
        self.turns: list[transcript.Turn] = []
        self.extra_speakers: dict[str, str] = {}
        self.rows: dict[int, TurnRow] = {}
        self.speaker_rows: dict[str, SpeakerRow] = {}
        self.visible_index: dict[int, int] = {}
        self.active_turns: set[int] = set()
        self.current: tuple[int, int] | None = None  # (turn index, word index)
        self.matches: list[tuple[int, int]] = []
        self.match_set: set[tuple[int, int]] = set()
        self.match_cursor = -1
        self._generation = 0
        self._verify_cancel: threading.Event | None = None
        self._scrubbing = False
        self._scroll_animation: Adw.TimedAnimation | None = None
        self._inhibit_cookie = 0

        self._setup_actions()
        self._setup_transcript_list()
        self._setup_player()
        self._setup_drop_target()
        self._restore_state()

        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_: self.activate_action("win.next-match"))
        self.search_entry.connect("previous-match", lambda *_: self.activate_action("win.previous-match"))
        self.search_entry.connect("next-match", lambda *_: self.activate_action("win.next-match"))
        self.search_entry.connect("stop-search", lambda *_: self.search_bar.set_search_mode(False))
        self.transcript_dropdown.connect("notify::selected", self._on_transcript_selected)
        self.style_manager.connect("notify::dark", self._on_style_changed)
        self.style_manager.connect("notify::accent-color", self._on_style_changed)
        self.connect("close-request", self._on_close_request)
        self._narrow = False
        self.narrow_breakpoint.connect("apply", lambda *_: self._set_narrow(True))
        self.narrow_breakpoint.connect("unapply", lambda *_: self._set_narrow(False))
        self._update_actions()

    # Setup

    def _setup_actions(self):
        def add(name, callback, param=None):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
            action.connect("activate", callback)
            self.add_action(action)
            return action

        add("play-pause", lambda *_: self.toggle_playback())
        add("skip-back", lambda *_: self.player.skip(-SKIP_MS))
        add("skip-forward", lambda *_: self.player.skip(SKIP_MS))
        add("nudge-back", lambda *_: self.player.skip(-NUDGE_MS))
        add("nudge-forward", lambda *_: self.player.skip(NUDGE_MS))
        add("next-match", lambda *_: self._step_match(1))
        add("previous-match", lambda *_: self._step_match(-1))
        add("show-in-files", self._on_show_in_files)
        add("show-transcript", lambda *_: self.view_stack.set_visible_child_name("transcript"))
        add("show-summary", lambda *_: self.view_stack.set_visible_child_name("summary"))

        rate = Gio.SimpleAction.new_stateful("rate", GLib.VariantType.new("s"), GLib.Variant("s", "1"))
        rate.connect("change-state", self._on_rate_changed)
        self.add_action(rate)

        search = Gio.SimpleAction.new_stateful("search", None, GLib.Variant("b", False))
        search.connect("change-state", self._on_search_action)
        self.add_action(search)

        for key in ("show-sidebar", "show-properties", "follow-along"):
            self.add_action(self.settings.create_action(key))
        self.settings.bind("show-sidebar", self.sidebar_split, "show-sidebar", Gio.SettingsBindFlags.DEFAULT)
        self.settings.bind("show-properties", self.inspector_split, "show-sidebar", Gio.SettingsBindFlags.DEFAULT)
        self.settings.connect("changed::follow-along", self._on_follow_changed)

    def _setup_transcript_list(self):
        self.store = Gio.ListStore.new(TurnItem)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_item_setup)
        factory.connect("bind", self._on_item_bind)
        factory.connect("unbind", self._on_item_unbind)
        self.transcript_list.set_factory(factory)
        self.transcript_list.set_model(Gtk.NoSelection.new(self.store))

        # Scrolling by hand stops following along, as in any reader that
        # scrolls itself: otherwise the next word would yank the view back.
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_user_scroll)
        self.transcript_scroller.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-update", self._on_user_drag)
        self.transcript_scroller.get_vscrollbar().add_controller(drag)

    def _on_item_setup(self, _factory, item):
        item.set_activatable(False)
        item.set_focusable(False)
        item.set_child(TurnRow(self))

    def _on_item_bind(self, _factory, item):
        row = item.get_child()
        turn = item.get_item().turn
        row.bind(turn)
        self.rows[turn.index] = row

    def _on_item_unbind(self, _factory, item):
        row = item.get_child()
        if row.turn is not None and self.rows.get(row.turn.index) is row:
            del self.rows[row.turn.index]
        row.turn = None

    def _setup_player(self):
        self.player.connect("notify::position", self._on_position_changed)
        self.player.connect("notify::duration", self._on_duration_changed)
        self.player.connect("notify::playing", self._on_playing_changed)
        self.player.connect(
            "error", lambda _p, message: self.toast(_("Playback failed: {error}").format(error=message))
        )
        self.position_scale.connect("change-value", self._on_scale_change_value)
        press = Gtk.GestureClick()
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", lambda *_: setattr(self, "_scrubbing", True))
        press.connect("released", lambda *_: setattr(self, "_scrubbing", False))
        press.connect("stopped", lambda *_: setattr(self, "_scrubbing", False))
        self.position_scale.add_controller(press)

    def _setup_drop_target(self):
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop)
        self.add_controller(target)

    def _restore_state(self):
        width = self.settings.get_int("window-width")
        height = self.settings.get_int("window-height")
        if width > 0 and height > 0:
            self.set_default_size(width, height)
        if self.settings.get_boolean("window-maximized"):
            self.maximize()

    def _on_close_request(self, *_args):
        if self.get_application().confirm_capture_close(self):
            return True
        self.get_application().window_closing(self)
        width, height = self.get_default_size()
        self.settings.set_int("window-width", width)
        self.settings.set_int("window-height", height)
        self.settings.set_boolean("window-maximized", self.is_maximized())
        self._cancel_verification()
        self._uninhibit()
        self.player.shutdown()
        return False

    # Loading

    @property
    def has_meeting(self):
        return self.file is not None

    def open_file(self, file: Gio.File):
        path = file.get_path()
        if path is None:
            self.toast(_("Only local files can be opened"))
            return
        self._cancel_verification()
        self._generation += 1
        generation = self._generation
        self.file = file
        self.main_stack.set_visible_child_name("loading")
        self.set_title(file.get_basename())

        def work():
            try:
                meeting = portable.read_meeting(path)
            except (OSError, portable.NotOpus, portable.Truncated) as exc:
                GLib.idle_add(self._on_load_failed, generation, file, str(exc))
                return
            words, error = None, None
            if meeting.is_readable and meeting.default_transcript_id:
                try:
                    words = portable.load_words(meeting, meeting.default_transcript_id)
                except portable.CassiniError as exc:
                    error = str(exc)
            GLib.idle_add(self._on_loaded, generation, file, meeting, words, error)

        threading.Thread(target=work, daemon=True).start()

    def _on_load_failed(self, generation, file, message):
        if generation != self._generation:
            return
        self.file = None
        self.main_stack.set_visible_child_name("empty")
        self.set_title(text("app.name"))
        self.toast(_("“{name}” is not an Opus audio file").format(name=file.get_basename()))
        self._update_actions()

    def _on_loaded(self, generation, file, meeting, words, error):
        if generation != self._generation:
            return
        self.meeting = meeting
        self.audio_check = None
        self.extra_speakers = {}
        self.palette = SpeakerPalette([s.id for s in meeting.speakers])
        self.transcript_id = meeting.default_transcript_id
        self.player.load(file.get_uri(), meeting.duration_ms)
        self.player.set_rate(float(self.get_action_state("rate").get_string()))

        title = meeting.title or file.get_basename()
        self.set_title(title)
        self.window_title.set_title(title)
        self.title_label.set_label(title)
        self.file_label.set_label(file.get_basename())
        self.file_label.set_tooltip_text(file.get_basename())

        self._populate_transcript_chooser()
        self._set_words(words, error)
        self._populate_summary()
        self._update_banner()
        self._populate_properties()
        self._update_subtitle()
        self.search_bar.set_search_mode(False)
        self.view_stack.set_visible_child_name("transcript")
        self.main_stack.set_visible_child_name("meeting")
        self._update_actions()
        if meeting.is_readable:
            self._start_verification(generation, file.get_path(), meeting)

    def _set_words(self, words, error=None):
        self.transcript_error = error
        self.turns = transcript.build_turns(words or [])
        known = {s.id for s in self.meeting.speakers} if self.meeting else set()
        for turn in self.turns:
            if turn.speaker not in known and turn.speaker not in self.extra_speakers:
                self.extra_speakers[turn.speaker] = _("Unknown Speaker")
        self.palette.extend(self.extra_speakers)
        self.active_turns = set()
        self.current = None
        self._populate_speakers()
        self._rebuild_store()
        self._refresh_search()

    def _populate_transcript_chooser(self):
        meeting = self.meeting
        entries = meeting.transcripts if meeting and meeting.is_readable else []
        self._chooser_ids = [e.id for e in entries]
        model = Gtk.StringList()
        for entry in entries:
            label = entry.id
            if entry.language:
                label = f"{label} ({entry.language})"
            model.append(label)
        self.transcript_dropdown.set_model(model)
        if self.transcript_id in self._chooser_ids:
            self.transcript_dropdown.set_selected(self._chooser_ids.index(self.transcript_id))
        self.transcript_chooser.set_visible(len(entries) > 1)

    def _on_transcript_selected(self, dropdown, _pspec):
        index = dropdown.get_selected()
        if not self.meeting or index >= len(getattr(self, "_chooser_ids", [])):
            return
        transcript_id = self._chooser_ids[index]
        if transcript_id == self.transcript_id:
            return
        self.transcript_id = transcript_id
        try:
            words, error = portable.load_words(self.meeting, transcript_id), None
        except portable.CassiniError as exc:
            words, error = None, str(exc)
        self._set_words(words, error)
        self._populate_properties()
        self._update_subtitle()

    def _start_verification(self, generation, path, meeting):
        cancel = threading.Event()
        self._verify_cancel = cancel

        def work():
            try:
                check = portable.verify_audio(path, meeting, cancel.is_set)
            except InterruptedError:
                return
            GLib.idle_add(self._on_verified, generation, check)

        threading.Thread(target=work, daemon=True).start()

    def _cancel_verification(self):
        if self._verify_cancel is not None:
            self._verify_cancel.set()
            self._verify_cancel = None

    def _on_verified(self, generation, check):
        if generation != self._generation:
            return
        self.audio_check = check
        self._verify_cancel = None
        self._update_banner()
        self._populate_properties()

    @property
    def state(self) -> State | None:
        if self.audio_check is not None:
            return self.audio_check.state
        return self.meeting.state if self.meeting else None

    # Speakers

    def speaker_label(self, speaker_id):
        if self.meeting:
            label = self.meeting.speaker_label(speaker_id)
            if label is not None:
                return label or speaker_id
        return self.extra_speakers.get(speaker_id, speaker_id)

    def _speaker_ids(self):
        ids = [s.id for s in self.meeting.speakers] if self.meeting and self.meeting.is_readable else []
        return ids + [i for i in self.extra_speakers if i not in ids]

    def _populate_speakers(self):
        self.speakers_list.remove_all()
        self.speaker_rows = {}
        ids = self._speaker_ids()
        for speaker_id in ids:
            row = SpeakerRow(speaker_id, self.speaker_label(speaker_id), self.palette.css_class(speaker_id))
            self.speaker_rows[speaker_id] = row
            self.speakers_list.append(row)
        self.speakers_heading.set_visible(bool(ids))

    # The transcript list

    def _rebuild_store(self):
        visible = transcript.visible_turns(self.turns)
        self.visible_index = {turn.index: i for i, turn in enumerate(visible)}
        self.rows = {}
        self.store.splice(0, self.store.get_n_items(), [TurnItem(t) for t in visible])
        self._update_transcript_page(bool(visible))

    def _update_transcript_page(self, has_rows):
        meeting = self.meeting
        status = self.transcript_status
        if has_rows:
            self.transcript_stack.set_visible_child_name("list")
            return
        if meeting is None:
            return
        if meeting.state == State.PLAIN_AUDIO:
            status.set_icon_name("audio-x-generic-symbolic")
            status.set_title(_("No Transcript"))
            status.set_description(
                _("This is an ordinary audio file without a Cassini transcript. It can still be played.")
            )
        elif meeting.state == State.UNKNOWN_FORMAT:
            status.set_icon_name("dialog-information-symbolic")
            status.set_title(_("Newer Format"))
            status.set_description(
                _(
                    "This file was made with a newer version of the Cassini format. Its audio can be played, but its transcript can’t be shown."
                )
            )
        elif meeting.state == State.INVALID:
            status.set_icon_name("dialog-warning-symbolic")
            status.set_title(_("Transcript Can’t Be Read"))
            status.set_description(
                GLib.markup_escape_text(
                    _("The transcript data in this file is damaged: {reason}. The audio can still be played.").format(
                        reason=meeting.reason
                    )
                )
            )
        elif self.transcript_error:
            status.set_icon_name("dialog-warning-symbolic")
            status.set_title(_("Transcript Unavailable"))
            status.set_description(
                GLib.markup_escape_text(
                    _("The transcript “{id}” can’t be loaded: {reason}").format(
                        id=self.transcript_id, reason=self.transcript_error
                    )
                )
            )
        else:
            status.set_icon_name("view-list-symbolic")
            status.set_title(_("Empty Transcript"))
            status.set_description(_("Nobody said anything in this transcript."))
        self.transcript_stack.set_visible_child_name("status")

    def turn_markup(self, turn):
        """Pango markup for one turn's words, and each word's byte offset."""
        dark = self.style_manager.get_dark()
        accent = hex_color(self.style_manager.get_accent_color_rgba())
        current_alpha = "45%" if dark else "25%"
        match_bg, match_alpha = ("#f8e45c", "40%") if dark else ("#f6d32d", "60%")
        position = self.player.position
        focused = self.matches[self.match_cursor] if 0 <= self.match_cursor < len(self.matches) else None

        displayed = {owner.index: owner.items for owner in [turn] + [aside for _, aside in turn.asides]}

        def word_link(owner, i):
            word = owner.words[i]
            current = owner.word_at(position) if owner.index in self.active_turns else -1
            attrs = []
            key = (owner.index, i)
            if key == focused:
                attrs.append('background="#e5a50a" bgalpha="80%"')
            elif key in self.match_set:
                attrs.append(f'background="{match_bg}" bgalpha="{match_alpha}"')
            elif i == current:
                attrs.append(f'background="{accent}" bgalpha="{current_alpha}"')
            if current >= 0 and i > current:
                attrs.append('fgalpha="60%"')
            if word.low_confidence:
                attrs.append('style="italic"')
            text = GLib.markup_escape_text(displayed[owner.index][i])
            inner = f"<span {' '.join(attrs)}>{text}</span>" if attrs else text
            return f'<a href="w:{owner.index}/{i}">{inner}</a>'

        def chip(aside):
            name = self.speaker_label(aside.speaker)
            on = aside.is_active(position)
            bg = accent if on else ("#ffffff" if dark else "#000006")
            alpha = current_alpha if on else ("10%" if dark else "7%")
            links = " ".join(word_link(aside, j) for j, value in enumerate(displayed[aside.index]) if value)
            markup = (
                f'<span size="small" background="{bg}" bgalpha="{alpha}">'
                f" <b>{GLib.markup_escape_text(name)}:</b> {links} </span>"
            )
            return markup, f" {name}: {aside.text} "

        return transcript.join_items(turn, word_link, chip)

    def _render_turns(self, indexes):
        for index in indexes:
            turn = self.turns[index] if 0 <= index < len(self.turns) else None
            if turn is None:
                continue
            host = turn.host.index if turn.host is not None else index
            row = self.rows.get(host)
            if row is not None:
                row.render()

    def _on_style_changed(self, *_args):
        self._render_turns(list(self.rows))

    # Playback

    def toggle_playback(self):
        if self.has_meeting:
            self.player.toggle()

    def seek_and_play(self, ms):
        self.player.seek(ms)
        if not self.player.playing:
            self.player.play()

    def seek_to_word(self, turn_index, word_index):
        word = self.turns[turn_index].words[word_index]
        self.seek_and_play(word.start_ms + 1)

    def _on_position_changed(self, *_args):
        self._update_position()

    def _on_duration_changed(self, *_args):
        self.position_adjustment.set_upper(max(1, self.player.duration))
        self._update_position(force=True)
        self._update_subtitle()
        if self.meeting is not None and self.meeting.duration_ms is None:
            self._populate_properties()

    def _on_playing_changed(self, *_args):
        playing = self.player.playing
        self.play_button.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        self.play_button.set_tooltip_text(_("Pause") if playing else _("Play"))
        if playing:
            self._inhibit()
        else:
            self._uninhibit()

    def _inhibit(self):
        if not self._inhibit_cookie:
            self._inhibit_cookie = self.get_application().inhibit(
                self, Gtk.ApplicationInhibitFlags.SUSPEND, _("Playing a meeting")
            )

    def _uninhibit(self):
        if self._inhibit_cookie:
            self.get_application().uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0

    def _on_scale_change_value(self, _scale, _scroll, value):
        self.player.seek(int(value))
        return False

    def _update_position(self, force=False):
        position = self.player.position
        duration = self.player.duration
        if not self._scrubbing or force:
            self.position_adjustment.set_value(position)
        self.elapsed_label.set_label(format_time(position))
        self.remaining_label.set_label("−" + format_time(max(0, duration - position)))

        active = {t.index for t in self.turns if t.is_active(position)}
        turn = transcript.current_turn(self.turns, position)
        current = (turn.index, turn.word_at(position)) if turn else None
        asides = {index for index in active if self.turns[index].host is not None}
        if current == self.current and active == self.active_turns and not force and not asides:
            return
        changed = (active ^ self.active_turns) | asides
        if self.current:
            changed.add(self.current[0])
        if current:
            changed.add(current[0])
        self.active_turns = active
        self.current = current
        self._render_turns(changed)
        speaking = {self.turns[i].speaker for i in active}
        for speaker_id, row in self.speaker_rows.items():
            row.set_speaking(speaker_id in speaking)
        if current and self.settings.get_boolean("follow-along") and self.player.playing:
            self._scroll_to_word(*current)

    # Following along

    def _scroll_to_word(self, turn_index, word_index, center=False):
        turn = self.turns[turn_index]
        if turn.host is not None:
            turn_index, word_index = turn.host.index, max(0, turn.host.word_at(turn.start_ms))
        position = self.visible_index.get(turn_index)
        if position is None:
            return
        row = self.rows.get(turn_index)
        if row is None or not row.get_mapped() or not row.offsets:
            self.transcript_list.scroll_to(position, Gtk.ListScrollFlags.NONE, None)
            GLib.idle_add(self._scroll_to_word_later, turn_index, word_index, center)
            return
        layout = row.words.get_layout()
        offset = row.offsets[min(max(word_index, 0), len(row.offsets) - 1)]
        rect = layout.index_to_pos(offset)
        point = Graphene.Point()
        point.init(0, rect.y / Pango.SCALE)
        ok, target = row.words.compute_point(self.transcript_scroller, point)
        if not ok:
            return
        height = self.transcript_scroller.get_height()
        line = rect.height / Pango.SCALE
        if not center and 48 <= target.y <= height - 96 - line:
            return
        adjustment = self.transcript_scroller.get_vadjustment()
        goal = adjustment.get_value() + target.y - height * 0.35
        goal = max(adjustment.get_lower(), min(goal, adjustment.get_upper() - adjustment.get_page_size()))
        self._animate_scroll(adjustment, goal)

    def _scroll_to_word_later(self, turn_index, word_index, center):
        row = self.rows.get(turn_index)
        if row is not None and row.get_mapped():
            self._scroll_to_word(turn_index, word_index, center)
        return GLib.SOURCE_REMOVE

    def _animate_scroll(self, adjustment, goal):
        if self._scroll_animation is not None:
            self._scroll_animation.skip()
        target = Adw.PropertyAnimationTarget.new(adjustment, "value")
        animation = Adw.TimedAnimation.new(self.transcript_scroller, adjustment.get_value(), goal, 300, target)
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._scroll_animation = animation
        animation.connect("done", lambda *_: setattr(self, "_scroll_animation", None))
        animation.play()

    def _on_user_scroll(self, *_args):
        self._stop_following()
        return False

    def _on_user_drag(self, *_args):
        self._stop_following()

    def _stop_following(self):
        if self.player.playing and self.settings.get_boolean("follow-along"):
            self.settings.set_boolean("follow-along", False)

    def _on_follow_changed(self, settings, _key):
        if settings.get_boolean("follow-along") and self.current:
            self._scroll_to_word(*self.current, center=True)

    # Search

    def _on_search_action(self, action, value):
        action.set_state(value)
        if self.search_bar.get_search_mode() != value.get_boolean():
            self.search_bar.set_search_mode(value.get_boolean())

    def _on_search_mode(self, bar, _pspec):
        enabled = bar.get_search_mode()
        self.lookup_action("search").set_state(GLib.Variant("b", enabled))
        if enabled:
            self.view_stack.set_visible_child_name("transcript")
            self.search_entry.grab_focus()
        else:
            self.search_entry.set_text("")
            self._refresh_search()

    def _on_search_changed(self, _entry):
        self._refresh_search()
        if self.matches:
            self.match_cursor = -1
            self._step_match(1, from_position=True)

    def _refresh_search(self):
        query = self.search_entry.get_text() if self.search_bar.get_search_mode() else ""
        old = {t for t, _w in self.match_set}
        self.matches = transcript.find_matches(self.turns, query)
        self.match_set = set(self.matches)
        self.match_cursor = -1
        self._update_match_label(query)
        self._render_turns(old | {t for t, _w in self.matches})

    def _update_match_label(self, query=None):
        if query is None:
            query = self.search_entry.get_text()
        if not query.strip():
            self.match_label.set_label("")
        elif not self.matches:
            self.match_label.set_label(_("No matches"))
        elif self.match_cursor >= 0:
            self.match_label.set_label(
                _("{current} of {total}").format(current=self.match_cursor + 1, total=len(self.matches))
            )
        else:
            count = len(self.matches)
            self.match_label.set_label(ngettext("{n} match", "{n} matches", count).format(n=number(count)))

    def _step_match(self, delta, from_position=False):
        if not self.matches:
            return
        previous = self.matches[self.match_cursor] if self.match_cursor >= 0 else None
        if from_position:
            position = self.player.position
            self.match_cursor = next(
                (i for i, (t, w) in enumerate(self.matches) if self.turns[t].words[w].start_ms >= position), 0
            )
        else:
            self.match_cursor = (self.match_cursor + delta) % len(self.matches)
        turn_index, word_index = self.matches[self.match_cursor]
        self._render_turns({turn_index} | ({previous[0]} if previous else set()))
        self._update_match_label()
        self._stop_following()
        self._scroll_to_word(turn_index, word_index, center=True)

    def _set_narrow(self, narrow):
        self._narrow = narrow
        self.switcher_bar.set_reveal(narrow and self.summary_page.get_visible())

    # Summary

    def _populate_summary(self):
        while child := self.summary_box.get_first_child():
            self.summary_box.remove(child)
        markdown = self.meeting.summary_markdown if self.meeting and self.meeting.is_readable else None
        self.summary_page.set_visible(bool(markdown))
        self.title_stack.set_visible_child_name("switcher" if markdown and not self._narrow else "title")
        self.switcher_bar.set_reveal(self._narrow and bool(markdown))
        if not markdown:
            return
        listbox = None
        for block in summary.parse(markdown):
            if block.kind == "item":
                if listbox is None:
                    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
                    listbox.set_margin_bottom(12)
                    self.summary_box.append(listbox)
                listbox.append(self._summary_row(block))
                continue
            listbox = None
            label = Gtk.Label(
                label=block.markup,
                use_markup=True,
                wrap=True,
                xalign=0,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
            )
            label.connect("activate-link", self._on_summary_link)
            if block.kind == "heading":
                label.add_css_class("title-2" if block.level == 1 else "heading")
                label.set_margin_top(12 if block.level == 1 else 6)
                label.set_accessible_role(Gtk.AccessibleRole.HEADING)
            else:
                label.add_css_class("body")
                label.add_css_class("summary-paragraph")
            self.summary_box.append(label)

    def _summary_row(self, block):
        row = Adw.ActionRow(title=block.markup, use_markup=True)
        row.set_title_lines(0)
        if block.checked is not None:
            icon = "checkbox-checked-symbolic" if block.checked else "checkbox-symbolic"
            image = Gtk.Image(icon_name=icon)
            image.update_property([Gtk.AccessibleProperty.LABEL], [_("Done") if block.checked else _("Not done")])
            row.add_prefix(image)
        if block.seek_ms is not None:
            row.add_suffix(Gtk.Label(label=format_time(block.seek_ms), css_classes=["numeric", "accent"]))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.set_activatable(True)
            row.connect("activated", lambda _row, ms=block.seek_ms: self._jump_from_summary(ms))
        return row

    def _on_summary_link(self, _label, uri):
        if uri.startswith("seek:"):
            self._jump_from_summary(int(uri[5:]))
            return True
        return False

    def _jump_from_summary(self, ms):
        self.view_stack.set_visible_child_name("transcript")
        self.seek_and_play(ms)
        if not self.settings.get_boolean("follow-along"):
            self.settings.set_boolean("follow-along", True)

    # Properties and status

    def _update_subtitle(self):
        meeting = self.meeting
        parts = []
        if meeting and meeting.is_readable:
            date = format_date(meeting.recorded_at_local, local=True) or format_date(meeting.created_at)
            if date:
                parts.append(date)
            count = len(self._speaker_ids())
            parts.append(ngettext("{n} speaker", "{n} speakers", count).format(n=number(count)))
        elif self.player.duration:
            parts.append(format_time(self.player.duration))
        self.subtitle_label.set_label(" · ".join(parts))

    def _update_banner(self):
        state = self.state
        if state == State.STALE_AUDIO:
            title = _(
                "The audio in this file does not match the transcript recorded with it. The transcript may describe a different recording."
            )
        elif state == State.INVALID:
            title = _("The transcript data in this file is damaged. Only the audio can be played.")
        elif state == State.UNKNOWN_FORMAT:
            title = _("This file uses a newer Cassini format. Only the audio can be played.")
        else:
            self.banner.set_revealed(False)
            return
        self.banner.set_title(title)
        self.banner.set_revealed(True)

    def _populate_properties(self):
        box = self.properties_box
        while child := box.get_first_child():
            box.remove(child)
        meeting = self.meeting
        if meeting is None:
            return

        box.append(self._trust_card())

        recording = Adw.PreferencesGroup(title=_("Recording"))
        duration = meeting.duration_ms or self.player.duration
        if duration:
            recording.add(property_row(_("Duration"), format_time(duration)))
        recorded = format_date(meeting.recorded_at_local, local=True, time=True)
        if recorded:
            recording.add(property_row(_("Recorded"), recorded))
        created = format_date(meeting.created_at, time=True)
        if created:
            recording.add(property_row(_("Processed"), created))
        channels = meeting.channels
        audio_format = {1: _("Ogg Opus, 48 kHz, mono"), 2: _("Ogg Opus, 48 kHz, stereo")}.get(
            channels, _("Ogg Opus, 48 kHz")
        )
        recording.add(property_row(_("Format"), audio_format))
        try:
            info = self.file.query_info("standard::size", Gio.FileQueryInfoFlags.NONE, None)
            recording.add(property_row(_("Size"), GLib.format_size(info.get_size())))
        except GLib.Error:
            pass
        box.append(recording)

        entry = next((e for e in meeting.transcripts if e.id == self.transcript_id), None)
        if meeting.is_readable and entry is not None:
            group = Adw.PreferencesGroup(title=_("Transcript"))
            group.add(property_row(_("Name"), entry.id, monospace=True))
            group.add(property_row(_("Schema"), entry.format, monospace=True))
            if entry.language:
                group.add(property_row(_("Language"), entry.language))
            if self.turns or not self.transcript_error:
                words = sum(len(t.words) for t in self.turns)
                group.add(property_row(_("Words"), f"{words:n}"))
            group.add(property_row(_("Speakers"), f"{len(self._speaker_ids()):n}"))
            group.add(property_row(_("Timing"), _("Per word, in milliseconds")))
            provenance = (meeting.manifest.get("provenance") or {}).get("speechToText")
            step = provenance.get(entry.id) if isinstance(provenance, dict) else None
            if isinstance(step, dict):
                engine = " · ".join(str(step[k]) for k in ("engine", "model") if isinstance(step.get(k), str))
                if engine:
                    group.add(property_row(_("Produced By"), engine))
            # Group the explanatory note with its section using positive
            # spacing. A negative label margin makes GTK's measured height
            # smaller than its minimum and triggers allocation warnings.
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            section.append(group)
            note = Gtk.Label(
                label=_(
                    "The transcript and speakers live inside this .opus file, so any audio player can still play it."
                ),
                wrap=True,
                xalign=0,
                css_classes=["caption", "dimmed"],
            )
            note.set_margin_start(2)
            section.append(note)
            box.append(section)

        buttons = Gtk.Box(spacing=8, homogeneous=True)
        buttons.append(Gtk.Button(label=_("Show in Files"), action_name="win.show-in-files"))
        box.append(buttons)

    def _trust_card(self):
        meeting = self.meeting
        state = self.state
        checking = meeting.is_readable and self.audio_check is None
        if checking:
            icon, title = None, _("Checking Audio…")
            subtitle = _("Comparing the audio with the fingerprint recorded in the transcript.")
        elif state == State.OK:
            icon, title = "verified-symbolic", _("Audio Matches Transcript")
            subtitle = _("The audio’s SHA-256 fingerprint matches the one recorded with the transcript.")
        elif state == State.STALE_AUDIO:
            icon, title = "dialog-warning-symbolic", _("Audio Does Not Match")
            subtitle = _("The file was probably re-encoded or edited after it was transcribed.")
        elif state == State.UNVERIFIED:
            icon, title = "dialog-question-symbolic", _("Not Verified")
            subtitle = (self.audio_check.reason if self.audio_check else None) or _("The audio was not checked.")
        elif state == State.INVALID:
            icon, title = "dialog-error-symbolic", _("Damaged Transcript Data")
            subtitle = meeting.reason or ""
        elif state == State.UNKNOWN_FORMAT:
            icon, title = "dialog-information-symbolic", _("Unsupported Version")
            subtitle = meeting.format_id or ""
        else:
            icon, title = "audio-x-generic-symbolic", _("Plain Audio")
            subtitle = _("This file carries no Cassini metadata.")
        card = Gtk.Box(spacing=12, css_classes=["card", "trust-card"])
        if icon is None:
            card.append(Adw.Spinner(valign=Gtk.Align.START))
        else:
            image = Gtk.Image(icon_name=icon, valign=Gtk.Align.START)
            image.add_css_class(
                {State.OK: "success", State.STALE_AUDIO: "warning", State.INVALID: "error"}.get(state, "dimmed")
            )
            card.append(image)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.append(Gtk.Label(label=title, xalign=0, wrap=True, css_classes=["heading"]))
        text.append(Gtk.Label(label=subtitle, xalign=0, wrap=True, css_classes=["caption", "dimmed"]))
        card.append(text)
        return card

    def _update_actions(self):
        has = self.has_meeting
        for name in ("play-pause", "skip-back", "skip-forward", "nudge-back", "nudge-forward", "show-in-files"):
            self.lookup_action(name).set_enabled(has)
        self.lookup_action("search").set_enabled(has and bool(self.turns))
        self.lookup_action("show-summary").set_enabled(self.summary_page.get_visible())

    # Rate, files, export

    def _on_rate_changed(self, action, value):
        action.set_state(value)
        rate = float(value.get_string())
        self.player.set_rate(rate)
        self.rate_button.set_label(f"{rate:g}×")

    def _on_drop(self, _target, value, _x, _y):
        files = value.get_files()
        if not files:
            return False
        self.open_file(files[0])
        app = self.get_application()
        for extra in files[1:]:
            app.open_in_new_window(extra)
        return True

    def _on_show_in_files(self, *_args):
        if self.file is None:
            return
        launcher = Gtk.FileLauncher.new(self.file)
        launcher.open_containing_folder(self, None, self._on_launcher_done)

    def _on_launcher_done(self, launcher, result):
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self.toast(_("Could not open the folder"))

    def toast(self, message):
        self.toast_overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(message)))


def property_row(title, value, monospace=False):
    row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value), subtitle_selectable=True)
    row.add_css_class("property")
    if monospace:
        row.add_css_class("monospace-value")
    return row


def format_date(value, local=False, time=False):
    """Format an RFC 3339 instant in local time, or a zone-less local label as written."""
    if not value:
        return None
    if local:
        # recordedAtLocal has no zone and is a label: show it as written.
        dt = GLib.DateTime.new_from_iso8601(value, GLib.TimeZone.new_utc())
    else:
        dt = GLib.DateTime.new_from_iso8601(value, None)
        dt = dt.to_local() if dt else None
    if dt is None:
        return None
    text = calendar_formats.full_date(date(dt.get_year(), dt.get_month(), dt.get_day_of_month()))
    if time:
        text = f"{text} {fmt.row_time(dt.to_unix() * 1000, tz=dt.get_timezone().get_identifier())}"
    return text
