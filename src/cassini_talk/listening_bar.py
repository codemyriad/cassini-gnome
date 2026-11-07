# SPDX-License-Identifier: GPL-3.0-or-later
"""D70 listening controls; no independent playback state or position timer."""

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import ux_formats as fmt
from .strings import text


def merge_choices(meeting, turn):
    """Neighbours of this listening turn first, then the pane's talk-time order."""
    others = {s.id: s for s in meeting.speakers if s.id != turn.speaker}
    neighbours = []
    for index, suffix in ((turn.index - 1, "speaksBefore"), (turn.index + 1, "speaksAfter")):
        if 0 <= index < len(meeting.listening_turns):
            speaker = others.pop(meeting.listening_turns[index].speaker, None)
            if speaker:
                neighbours.append((speaker, text("speakers.mergeInto." + suffix)))
    return neighbours, sorted(others.values(), key=lambda s: -s.talk_ms)


def excerpt(meeting, turn):
    before = meeting.listening_turns[turn.index - 1] if turn.index else None
    previous = " ".join(before.text.split()[-5:]) if before else ""
    current = " ".join(turn.text.split()[:8])
    return f"{previous} │ {current}" if previous else current


class ListeningBar(Gtk.Revealer):
    def __init__(self, page):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.page, self.listen = page, page.window.listen
        self._announcement = None
        self._layout = None
        self._menu_key = None
        self.body = Gtk.Box(spacing=4, css_classes=["listening-bar"], accessible_role=Gtk.AccessibleRole.GROUP)
        self.responsive = Adw.BreakpointBin(child=self.body, width_request=360, height_request=44)
        self.set_child(self.responsive)
        for width, layout in ((720, 1), (600, 2), (480, 3)):
            breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {width}sp"))
            breakpoint.connect("apply", lambda _b, layout=layout: self._apply_layout(layout))
            breakpoint.connect("unapply", lambda _b: self._apply_layout(0))
            self.responsive.add_breakpoint(breakpoint)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=2)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"], max_width_chars=25)
        self.turn = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "small"])
        info.append(self.name)
        info.append(self.turn)
        self.body.append(info)
        self.excerpt = Gtk.Label(
            ellipsize=Pango.EllipsizeMode.END, hexpand=True, max_width_chars=35, css_classes=["dim-label"]
        )
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
            self.buttons[key] = button
            self.body.append(button)
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
        if self.get_child_revealed() and self.listen.speaker in self.page._speaker_rows:
            self.page._scroll_speaker(self.page._speaker_rows[self.listen.speaker][0])

    def order(self):
        return [s.id for s in self.page.meeting.speakers]

    def step_speaker(self, direction):
        fn = self.listen.next_speaker if direction > 0 else self.listen.previous_speaker
        return fn(self.order())

    def name_speaker(self):
        if self.listen.active:
            self.page.player.pause()
            self.page.name_speakers(self.listen.speaker, from_listening=True)

    def _collapse(self, layout):
        editable = self.page.meeting.metadata_editable
        self.excerpt.set_visible(layout == 0)
        self.buttons["name"].set_visible(editable and layout < 2)
        self.merge.set_visible(editable and layout < 2)
        self.buttons["previousSpeaker"].set_visible(layout < 3)
        self.buttons["nextSpeaker"].set_visible(layout < 3)
        self.more_model.remove_all()
        if layout >= 2 and editable:
            self.more_model.append(text("listen.name"), "listening.name")
            self.more_model.append_submenu(text("speakers.mergeInto"), self.merge_model)
        if layout >= 3:
            self.more_model.append(text("listen.previousSpeaker"), "listening.previous-speaker")
            self.more_model.append(text("listen.nextSpeaker"), "listening.next-speaker")
        self.more.set_visible(self.more_model.get_n_items() > 0)

    def update(self):
        listen, meeting = self.listen, self.page.meeting
        self.set_reveal_child(listen.active)
        if not listen.active or listen.turn is None:
            self._announcement = None
            return
        turn = listen.turn
        speaker = next((s for s in meeting.speakers if s.id == listen.speaker), None)
        if speaker is None:
            return
        self.body.update_property([Gtk.AccessibleProperty.LABEL], [text("listen.bar.a11y", name=speaker.name)])
        self.name.set_label(speaker.name)
        self.name.set_tooltip_text(speaker.name)
        self.turn.set_label(
            text("listen.turn", index=listen.index + 1, count=listen.count, position=fmt.position(turn.start_ms))
        )
        self.excerpt.set_label(excerpt(meeting, turn))
        self.update_navigation()
        self.buttons["again"].set_visible(listen.finished or listen.cued)
        again_label = text("listen.again" if listen.finished else "common.play")
        self.buttons["again"].set_tooltip_text(again_label)
        self.buttons["again"].update_property([Gtk.AccessibleProperty.LABEL], [again_label])
        editable = meeting.metadata_editable and self.page._speaker_future is None
        self.buttons["name"].set_sensitive(editable)
        self.merge.set_sensitive(editable)
        self.actions.lookup_action("name").set_enabled(editable)
        self.actions.lookup_action("merge").set_enabled(editable)
        menu_key = meeting.speakers, meeting.listening_turns, turn.index
        if menu_key != self._menu_key:
            self._menu_key = menu_key
            self.merge_model.remove_all()
            neighbours, remaining = merge_choices(meeting, turn)
            for choices in ([(s, f"{s.name}, {suffix}") for s, suffix in neighbours], [(s, s.name) for s in remaining]):
                section = Gio.Menu()
                for target, caption in choices:
                    item = Gio.MenuItem.new(caption, None)
                    item.set_action_and_target_value("listening.merge", GLib.Variant("s", target.id))
                    section.append_item(item)
                if section.get_n_items():
                    self.merge_model.append_section(None, section)
        self._collapse(self._layout or 0)
        announcement = listen.speaker, listen.index, speaker.name
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
        listen = self.listen
        for key, direction in (("previousTurn", -1), ("nextTurn", 1)):
            self.buttons[key].set_sensitive(listen.step_target(direction) is not None)
        for key, action, direction in (("previousSpeaker", "previous-speaker", -1), ("nextSpeaker", "next-speaker", 1)):
            enabled = listen.speaker_step_target(self.order(), direction) is not None
            self.buttons[key].set_sensitive(enabled)
            self.actions.lookup_action(action).set_enabled(enabled)
