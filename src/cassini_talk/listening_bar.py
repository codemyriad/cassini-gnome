# SPDX-License-Identifier: GPL-3.0-or-later
"""D70 listening controls; no independent playback state or position timer."""

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import ux_formats as fmt
from .listening_bar_model import BREAKPOINTS, bar_layout, bar_state, excerpt, merge_choices
from .meeting_text import COLOURS, DARK_COLOURS
from .strings import text


class ListeningBar(Gtk.Revealer):
    def __init__(self, page):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.page, self.listen = page, page.window.listen
        self._announcement = None
        self._merge_focus = None
        self._layout = None
        self._menu_key = None
        self.body = Gtk.Box(spacing=4, css_classes=["listening-bar"], accessible_role=Gtk.AccessibleRole.GROUP)
        self.responsive = Adw.BreakpointBin(child=self.body, width_request=360, height_request=44)
        self.set_child(self.responsive)
        for width, layout in BREAKPOINTS:
            breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {width}sp"))
            breakpoint.connect("apply", lambda _b, layout=layout: self._apply_layout(layout))
            breakpoint.connect("unapply", lambda _b: self._apply_layout(0))
            self.responsive.add_breakpoint(breakpoint)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=2)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"], max_width_chars=25)
        self.turn = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "small"])
        title = Gtk.Box(spacing=6)
        self.dot = Gtk.Label(label="●", accessible_role=Gtk.AccessibleRole.PRESENTATION)
        title.append(self.dot)
        title.append(self.name)
        info.append(title)
        info.append(self.turn)
        self.body.append(info)
        self.excerpt = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, hexpand=True, max_width_chars=35)
        self.body.append(self.excerpt)
        self.buttons = {}
        for key, icon, callback in (
            ("previousTurn", "go-up-symbolic", self.listen.previous_turn),
            ("nextTurn", "go-down-symbolic", self.listen.next_turn),
            ("previousSpeaker", "go-previous-symbolic", lambda: self.step_speaker(-1)),
            ("nextSpeaker", "go-next-symbolic", lambda: self.step_speaker(1)),
            ("again", "media-playback-start-symbolic", self.listen.play_again),
            ("name", "document-edit-symbolic", self.name_speaker),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=text("listen." + key), css_classes=["flat"])
            button.update_property([Gtk.AccessibleProperty.LABEL], [text("listen." + key)])
            button.connect("clicked", lambda _b, fn=callback: fn())
            if key == "name":
                button.set_label(text("listen.name"))
            self.buttons[key] = button
            self.body.append(button)
        self.buttons["again"].connect("map", self._focus_merge)
        self.merge_model = Gio.Menu()
        self.merge = Gtk.MenuButton(label=text("speakers.mergeInto"), menu_model=self.merge_model, css_classes=["flat"])
        self.body.append(self.merge)
        self.more_model = Gio.Menu()
        self.more = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=self.more_model,
            css_classes=["flat"],
            tooltip_text=text("meeting.moreActions"),
        )
        self.body.append(self.more)
        self.stop = Gtk.Button(
            icon_name="window-close-symbolic", tooltip_text=text("listen.stop"), css_classes=["flat"]
        )
        self.stop.update_property([Gtk.AccessibleProperty.LABEL], [text("listen.stop")])
        self.stop.connect("clicked", lambda *_: page.stop_listening())
        self.body.append(self.stop)
        self.actions = Gio.SimpleActionGroup()
        for key, callback in (
            ("name", self.name_speaker),
            ("previous-speaker", lambda: self.step_speaker(-1)),
            ("next-speaker", lambda: self.step_speaker(1)),
        ):
            action = Gio.SimpleAction.new(key, None)
            action.connect("activate", lambda _a, _p, fn=callback: fn())
            self.actions.add_action(action)
        action = Gio.SimpleAction.new("merge", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, target: page.merge_speaker(self.listen.speaker, target.get_string()))
        self.actions.add_action(action)
        self.insert_action_group("listening", self.actions)
        self.connect("notify::child-revealed", self._revealed)

    def _apply_layout(self, layout):
        self._layout = layout
        self._collapse(layout)

    def _revealed(self, *_):
        self._focus_merge()
        if self.get_child_revealed() and self.listen.speaker in self.page._speaker_rows:
            self.page._scroll_speaker(self.page._speaker_rows[self.listen.speaker][0])

    def focus_merge(self, speaker):
        self._merge_focus = speaker
        self._focus_merge()

    def _focus_merge(self, *_):
        if self._merge_focus is None:
            return
        if not self.listen.cued or self.listen.speaker != self._merge_focus:
            self._merge_focus = None
            return
        if self.get_child_revealed() and self.buttons["again"].get_mapped() and self.buttons["again"].grab_focus():
            self._merge_focus = None

    def order(self):
        return [s.id for s in self.page.meeting.speakers]

    def step_speaker(self, direction):
        fn = self.listen.next_speaker if direction > 0 else self.listen.previous_speaker
        return fn(self.order())

    def name_focus_target(self):
        return (
            self.buttons["name"]
            if self.buttons["name"].get_visible()
            else self.more.get_first_child()
            if self.more.get_visible()
            else self.stop
        )

    def name_speaker(self):
        if self.listen.active:
            self.page.player.pause()
            self.page.name_speakers(self.listen.speaker, from_listening=True)

    def _collapse(self, layout):
        layout = bar_layout(layout, editable=self.page.meeting.metadata_editable)
        self.excerpt.set_visible(layout.excerpt)
        self.buttons["name"].set_visible(layout.edit_buttons)
        self.merge.set_visible(layout.edit_buttons)
        self.buttons["previousSpeaker"].set_visible(layout.speaker_buttons)
        self.buttons["nextSpeaker"].set_visible(layout.speaker_buttons)
        self.more_model.remove_all()
        for action in layout.menu_actions:
            if action == "merge":
                self.more_model.append_submenu(text("speakers.mergeInto"), self.merge_model)
            else:
                caption = {
                    "name": "listen.name",
                    "previous-speaker": "listen.previousSpeaker",
                    "next-speaker": "listen.nextSpeaker",
                }[action]
                self.more_model.append(text(caption), "listening." + action)
        self.more.set_visible(bool(layout.menu_actions))

    def _state(self):
        return bar_state(
            self.listen,
            self.order(),
            editable=self.page.meeting.metadata_editable,
            busy=self.page._speaker_future is not None,
        )

    def update(self):
        listen, meeting = self.listen, self.page.meeting
        state = self._state()
        self.set_reveal_child(state.visible)
        self.buttons["name"].set_sensitive(state.edits_enabled)
        self.merge.set_sensitive(state.merge_enabled)
        self.actions.lookup_action("name").set_enabled(state.edits_enabled)
        self.actions.lookup_action("merge").set_enabled(state.merge_enabled)
        self.buttons["again"].set_visible(state.again_visible)
        again_label = text(state.again_label)
        self.buttons["again"].set_label(again_label)
        self.buttons["again"].set_tooltip_text(again_label)
        self.buttons["again"].update_property([Gtk.AccessibleProperty.LABEL], [again_label])
        self.update_navigation()
        self._focus_merge()
        if not listen.active or listen.turn is None:
            self._announcement = None
            return
        turn = listen.turn
        speaker = meeting.speaker_for(listen.speaker)
        self.body.update_property([Gtk.AccessibleProperty.LABEL], [text("listen.bar.a11y", name=speaker.name)])
        palette = DARK_COLOURS if Adw.StyleManager.get_default().get_dark() else COLOURS
        colour = palette[getattr(speaker, "colour_index", 0) % len(palette)]
        self.dot.set_markup(f'<span foreground="{colour}">●</span>')
        self.name.set_label(speaker.name)
        self.name.set_tooltip_text(speaker.name)
        self.turn.set_label(
            text("listen.turn", index=listen.index + 1, count=listen.count, position=fmt.position(turn.start_ms))
        )
        self.excerpt.set_markup(excerpt(meeting, turn))
        menu_key = meeting.speakers, meeting.listening_turns, turn.index, meeting.metadata.get("speakerMerges")
        if menu_key != self._menu_key:
            self._menu_key = menu_key
            self.merge_model.remove_all()
            neighbours, remaining = merge_choices(meeting, turn)
            for choices in (
                [(s, f"{s.name}, {text(suffix)}") for s, suffix in neighbours],
                [(s, s.name) for s in remaining],
            ):
                section = Gio.Menu()
                for target, caption in choices:
                    item = Gio.MenuItem.new(caption, None)
                    item.set_action_and_target_value("listening.merge", GLib.Variant("s", target.id))
                    section.append_item(item)
                if section.get_n_items():
                    self.merge_model.append_section(None, section)
        merge_enabled = state.merge_enabled and self.merge_model.get_n_items() > 0
        self.merge.set_sensitive(merge_enabled)
        self.actions.lookup_action("merge").set_enabled(merge_enabled)
        self._collapse(self._layout or 0)
        if self.page._updating_rows or speaker.id not in self.page._speaker_rows:
            return
        announcement = listen.speaker, listen.index
        if announcement != self._announcement:
            changed_speaker = self._announcement is None or self._announcement[0] != listen.speaker
            self._announcement = announcement
            message = (
                f"{speaker.name}, {self.page._speaker_rows.get(speaker.id, (None, None, ''))[2]}"
                if changed_speaker
                else text(
                    "listen.turn.announce",
                    index=listen.index + 1,
                    count=listen.count,
                    position=fmt.position(turn.start_ms),
                )
            )
            self.body.announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def update_navigation(self):
        state = self._state()
        for key, enabled in (
            ("previousTurn", state.previous_turn),
            ("nextTurn", state.next_turn),
            ("previousSpeaker", state.previous_speaker),
            ("nextSpeaker", state.next_speaker),
        ):
            self.buttons[key].set_sensitive(enabled)
        self.actions.lookup_action("previous-speaker").set_enabled(state.previous_speaker)
        self.actions.lookup_action("next-speaker").set_enabled(state.next_speaker)
