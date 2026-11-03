# SPDX-License-Identifier: GPL-3.0-or-later
"""D60 (R83): the test recording's dialog, over Settings or the first-run step.

It follows app.sound_check: test.running while it records, then one line per
stream with Play back, Test again and Done. Closing it (Done, Escape, its
parent closing, or a real Start discarding the test) deletes the test.
"""

import logging

from gi.repository import Adw, Gtk

from .sound_check import KINDS, REFUSED, SILENT, WORKING, result_line
from .strings import text

LOG = logging.getLogger(__name__)
STREAM_ICONS = {"microphone": "audio-input-microphone-symbolic", "system": "video-display-symbolic"}
STATE_ICONS = {
    WORKING: ("object-select-symbolic", "success"),
    SILENT: ("dialog-information-symbolic", "dim-label"),
    REFUSED: ("dialog-warning-symbolic", "warning"),
}


def icon(name, *classes):
    # The line says it all; the icons are decoration for screen readers.
    return Gtk.Image(icon_name=name, css_classes=list(classes), accessible_role=Gtk.AccessibleRole.PRESENTATION)


class SoundCheckDialog(Adw.Dialog):
    def __init__(self, app, parent):
        super().__init__(title=text("test.action"), content_width=460, follows_content_size=True)
        self.app, self.check = app, app.sound_check
        self.player = None
        self.closed = False
        self._again = False
        self._shown = None
        self.lines = []
        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        toolbar.set_content(self.stack)
        running = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=24, margin_end=24, margin_bottom=30
        )
        running.append(Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER))
        self.status = Gtk.Label(label=text("test.running"), wrap=True, justify=Gtk.Justification.CENTER)
        running.append(self.status)
        self.stack.add_named(running, "running")
        result = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.rows.update_property([Gtk.AccessibleProperty.LABEL], [text("test.action")])
        result.append(self.rows)
        self.note = Gtk.Label(label=text("test.note"), xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        result.append(self.note)
        self.stack.add_named(result, "result")
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
        self.done_button = Gtk.Button(label=text("common.done"), css_classes=["suggested-action"])
        self.done_button.connect("clicked", lambda *_: self.close())
        for button in (self.play_button, self.again_button, self.done_button):
            self.buttons.append(button)
        toolbar.add_bottom_bar(self.buttons)
        self.set_default_widget(self.done_button)
        self._handler = self.check.connect("changed", self._changed)
        # A test belongs to the dialog it was started from (Settings, first run).
        self._parent = parent if isinstance(parent, Adw.Dialog) else None
        self._parent_handler = self._parent.connect("closed", lambda *_: self.close()) if self._parent else 0
        self.connect("closed", self._closed)
        self._render()
        self.present(parent)

    def _changed(self, check):
        if check.state == "idle":
            if not self._again:
                self.close()  # Done, or a real Start discarded the test: no result.
            return
        self._render()

    def _render(self):
        check = self.check
        running = check.state in ("running", "judging")
        if running:
            self._stop_player()
        self.stack.set_visible_child_name("running" if running else "result")
        self.buttons.set_visible(not running)
        self.again_button.set_sensitive(not running and self.app.can_test_recording())
        if check.state != "result" or check.outcomes is self._shown:
            return
        self._shown = check.outcomes
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        self.lines = []
        for kind in KINDS:
            outcome = check.outcomes[kind]
            line = result_line(kind, outcome)
            name, style = STATE_ICONS[outcome.state]
            row = Adw.ActionRow(title=line, use_markup=False, title_lines=0)
            row.add_prefix(icon(STREAM_ICONS[kind]))
            row.add_suffix(icon(name, style))
            self.rows.append(row)
            self.lines.append(line)
        playable = bool(check.tracks()[0])
        self.play_button.set_sensitive(playable)
        self.note.set_visible(playable)
        self.done_button.grab_focus()
        # Screen readers read the result when it replaces test.running.
        self.announce("\n".join(self.lines), Gtk.AccessibleAnnouncementPriority.MEDIUM)

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
        self.check.disconnect(self._handler)
        if self._parent_handler:
            self._parent.disconnect(self._parent_handler)
            self._parent_handler = 0
        self._stop_player()
        self.check.close()
        if self.app.sound_check_dialog is self:
            self.app.sound_check_dialog = None
