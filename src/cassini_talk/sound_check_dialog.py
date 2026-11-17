# SPDX-License-Identifier: GPL-3.0-or-later
"""D60 (R83), D104 §2: the test recording's dialog, over Settings or Before you record.

One screen for testing and the result. A plain header bar with the title;
under it the red Recording pill with the time while testing, then "Play it
back…" on the result. Two rows (prompt over source) show a live level and
"Waiting for sound" / "Hearing…" while testing, then a Working or No sound
heard pill. Play back, Test again and Done stay in place; while testing the
first two are greyed and Cancel takes Done's place. Closing it (Done, Cancel,
Escape, its parent closing, or a real Start discarding the test) deletes the
test.
"""

import logging

from gi.repository import Adw, GLib, Gtk

from . import ux_formats
from .sound_check import KINDS, WORKING, heard, pill_text, refusal_line
from .status_badge import BadgeGlyph, StatusBadge
from .strings import text

LOG = logging.getLogger(__name__)
STREAM_ICONS = {"microphone": "audio-input-microphone-symbolic", "system": "video-display-symbolic"}
PROMPTS = {"microphone": "test.mic.prompt", "system": "test.sound.prompt"}
SOURCES = {"microphone": "common.yourMicrophone", "system": "recordFirst.sound.title"}
# D104 §2: "Hearing you" on the microphone; the computer sound's is provisional.
HEARING = {"microphone": "test.live.hearing", "system": "test.live.hearingSound"}
TICK_MS = 100


class ResultPill(Gtk.Box):
    """D104 §2 result pill: Working (success tint, tick) or No sound heard (error tint, alert)."""

    def __init__(self):
        super().__init__(spacing=4, valign=Gtk.Align.CENTER, halign=Gtk.Align.END, css_classes=["badge", "test-pill"])
        self.tick = Gtk.Image(
            icon_name="object-select-symbolic", pixel_size=12, accessible_role=Gtk.AccessibleRole.PRESENTATION
        )
        self.alert = BadgeGlyph("alert")
        self.label = Gtk.Label(valign=Gtk.Align.CENTER, css_classes=["badge-label"])
        for child in (self.tick, self.alert, self.label):
            self.append(child)
        self.state = None

    def set_outcome(self, outcome):
        working = outcome.state == WORKING
        self.state = outcome.state
        self.tick.set_visible(working)
        self.alert.set_visible(not working)
        for name, on in (("test-pill-working", working), ("test-pill-silent", not working)):
            (self.add_css_class if on else self.remove_css_class)(name)
        self.label.set_label(pill_text(outcome))


class StreamRow(Gtk.ListBoxRow):
    """One stream: icon, prompt over source, then a live level or the result pill."""

    def __init__(self, kind):
        super().__init__(activatable=False, selectable=False, focusable=False)
        self.kind = kind
        box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=10, margin_bottom=10)
        self.set_child(box)
        box.append(
            Gtk.Image(
                icon_name=STREAM_ICONS[kind],
                pixel_size=20,
                valign=Gtk.Align.CENTER,
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.prompt = Gtk.Label(label=text(PROMPTS[kind]), xalign=0, wrap=True, css_classes=["heading"])
        self.source = Gtk.Label(label=text(SOURCES[kind]), xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        labels.append(self.prompt)
        labels.append(self.source)
        box.append(labels)
        self.stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False, valign=Gtk.Align.CENTER)
        live = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        self.status = Gtk.Label(xalign=1, css_classes=["dim-label", "caption"])
        self.level = Gtk.LevelBar(
            min_value=0, max_value=1, width_request=72, valign=Gtk.Align.CENTER, css_classes=["test-level"]
        )
        for offset in ("low", "high", "full"):
            self.level.remove_offset_value(offset)  # One tone, no traffic light.
        self.level.update_property([Gtk.AccessibleProperty.LABEL], [text(SOURCES[kind])])
        live.append(self.status)
        live.append(self.level)
        self.stack.add_named(live, "live")
        self.pill = ResultPill()
        self.stack.add_named(self.pill, "pill")
        box.append(self.stack)
        self.heard = False
        self.line = None

    def show_live(self, level):
        self.heard = self.heard or heard(level)
        self.level.set_value(max(0.0, min(1.0, level)))
        self.status.set_label(text(HEARING[self.kind] if self.heard else "test.live.waiting"))
        self.stack.set_visible_child_name("live")
        self.source.set_label(text(SOURCES[self.kind]))
        self._refused(False)
        self.update_property([Gtk.AccessibleProperty.LABEL], [text(PROMPTS[self.kind])])

    def _refused(self, refused):
        (self.source.add_css_class if refused else self.source.remove_css_class)("error")
        (self.source.remove_css_class if refused else self.source.add_css_class)("dim-label")

    def show_result(self, outcome):
        self.heard = False
        self.pill.set_outcome(outcome)
        self.stack.set_visible_child_name("pill")
        refused = refusal_line(self.kind, outcome)
        # A refused stream keeps its D58 line (built, not drawn) under the prompt.
        self.source.set_label(refused or text(SOURCES[self.kind]))
        self._refused(bool(refused))
        # Read as two lines, never a composed sentence: the source (or why it failed), then the pill.
        self.line = (refused or text(SOURCES[self.kind])) + "\n" + self.pill.label.get_label()
        self.update_property([Gtk.AccessibleProperty.LABEL], [self.line])


class SoundCheckDialog(Adw.Dialog):
    def __init__(self, app, parent):
        super().__init__(title=text("test.action"), content_width=480, follows_content_size=True)
        self.app, self.check = app, app.sound_check
        self.player = None
        self.closed = False
        self._again = False
        self._shown = None
        self._ticker = 0
        self.lines = []
        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())  # D104 §2: plain, the title only.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_start=24, margin_end=24)
        toolbar.set_content(content)
        # The line under the title: the Recording pill while testing, then test.note.
        self.subline = Gtk.Stack(hhomogeneous=False, vhomogeneous=True, halign=Gtk.Align.CENTER)
        self.pill = StatusBadge("recording", text("states.badge.recording", liveBadge=ux_formats.live_badge(0)))
        self.pill.set_halign(Gtk.Align.CENTER)
        self.subline.add_named(self.pill, "recording")
        self.note = Gtk.Label(
            label=text("test.note"), wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]
        )
        self.subline.add_named(self.note, "note")
        self.subline.add_named(Gtk.Box(), "none")
        content.append(self.subline)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.rows.update_property([Gtk.AccessibleProperty.LABEL], [text("test.action")])
        self.stream_rows = {kind: StreamRow(kind) for kind in KINDS}
        for row in self.stream_rows.values():
            self.rows.append(row)
        content.append(self.rows)
        self.buttons = Gtk.Box(
            spacing=10, homogeneous=True, margin_start=24, margin_end=24, margin_top=18, margin_bottom=24
        )
        self.play_button = Gtk.Button(
            child=Adw.ButtonContent(icon_name="media-playback-start-symbolic", label=text("test.playBack"))
        )
        self.play_button.update_property([Gtk.AccessibleProperty.LABEL], [text("test.playBack")])
        self.play_button.connect("clicked", self._play)
        self.again_button = Gtk.Button(label=text("test.again"))
        self.again_button.connect("clicked", self._test_again)
        # Done on the result; Cancel takes its place while testing. Either closes and deletes.
        self.done_button = Gtk.Button(label=text("common.done"))
        self.done_button.connect("clicked", lambda *_: self.close())
        for button in (self.play_button, self.again_button, self.done_button):
            self.buttons.append(button)
        toolbar.add_bottom_bar(self.buttons)
        self.set_default_widget(self.done_button)
        self._handler = self.check.connect("changed", self._changed)
        # A test belongs to the dialog it was started from (Settings, Before you record).
        self._parent = parent if isinstance(parent, Adw.Dialog) else None
        self._parent_handler = self._parent.connect("closed", lambda *_: self.close()) if self._parent else 0
        self.connect("closed", self._closed)
        self._render()
        self.present(parent)

    @property
    def running(self):
        return self.check.state in ("running", "judging")

    def _changed(self, check):
        if check.state == "idle":
            if not self._again:
                self.close()  # Done, or a real Start discarded the test: no result.
            return
        self._render()

    def _render(self):
        check = self.check
        running = self.running
        if running:
            self._stop_player()
            if self._shown is not None or not self._ticker:
                self._shown = None
                for row in self.stream_rows.values():
                    row.heard = False
            self._tick()
            if not self._ticker:
                self._ticker = GLib.timeout_add(TICK_MS, self._tick)
        else:
            self._stop_ticker()
        self.play_button.set_sensitive(False)
        self.again_button.set_sensitive(not running and self.app.can_test_recording())
        self.done_button.set_label(text("common.cancel" if running else "common.done"))
        (self.done_button.remove_css_class if running else self.done_button.add_css_class)("suggested-action")
        if running:
            self.subline.set_visible_child_name("recording")
            return
        if check.state != "result":
            return
        playable = bool(check.tracks()[0])
        self.play_button.set_sensitive(playable)
        self.subline.set_visible_child_name("note" if playable else "none")
        if check.outcomes is self._shown:
            return
        self._shown = check.outcomes
        self.lines = []
        for kind in KINDS:
            row = self.stream_rows[kind]
            row.show_result(check.outcomes[kind])
            self.lines.append(row.line)
        self.done_button.grab_focus()
        # Screen readers read the result when it replaces the live levels.
        self.announce("\n".join(self.lines), Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _tick(self):
        if not self.running or self.closed:
            self._ticker = 0
            return False
        self.pill.set_label(text("states.badge.recording", liveBadge=ux_formats.live_badge(self.check.elapsed_ms)))
        if self.check.state == "running":  # Judging keeps the last levels.
            for kind, level in zip(KINDS, self.check.levels, strict=True):
                self.stream_rows[kind].show_live(level)
        return True

    def _stop_ticker(self):
        if self._ticker:
            GLib.source_remove(self._ticker)
            self._ticker = 0

    def _test_again(self, *_):
        self._again = True  # Its discard of the shown result is not a close.
        try:
            started = self.app.start_test_recording()
        finally:
            self._again = False
        if not started:
            self.close()

    def _new_player(self):
        from .folder_player import FolderPlayer

        return FolderPlayer()

    def _play(self, *_):
        if self.player is not None:
            if self.player.playing:
                self.player.seek(0)
            else:
                self.player.play()
            return
        tracks, duration = self.check.tracks()
        if not tracks:
            return
        # Both streams together, never on top of a meeting.
        for window in self.app.get_windows():
            if getattr(window, "player", None) is not None:
                window.player.pause()
        player = self.player = self._new_player()
        player.connect("error", lambda *_: self._stop_player())
        self.app.watch_player(player)
        try:
            player.load_tracks(tracks, duration)
        except (OSError, ValueError, RuntimeError):
            LOG.exception("The test recording could not be played")
            self._stop_player()
            return
        player.play()

    def _stop_player(self):
        if self.player is not None:
            self.player.shutdown()
            self.player = None

    def _closed(self, *_):
        self.closed = True
        self._stop_ticker()
        self.check.disconnect(self._handler)
        if self._parent_handler:
            self._parent.disconnect(self._parent_handler)
            self._parent_handler = 0
        self._stop_player()
        self.check.close()
        if self.app.sound_check_dialog is self:
            self.app.sound_check_dialog = None
