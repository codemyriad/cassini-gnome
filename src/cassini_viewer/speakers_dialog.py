# SPDX-License-Identifier: GPL-3.0-or-later
"""UX16 speaker naming and merge dialog; metadata IO uses the Library worker."""

import logging
import re
from concurrent.futures import CancelledError

from gi.repository import Adw, Gio, GLib, Gtk

from . import speaker_edits, ux_formats
from .meeting_text import COLOURS, DARK_COLOURS
from .strings import text

LOG = logging.getLogger(__name__)


class SpeakersDialog(Adw.Dialog):
    def __init__(self, page, identifier):
        super().__init__(title=text("common.speakers"), content_width=520, css_classes=["speakers-dialog"])
        self.page, self.window = page, page.window
        self.meeting, self.identifier = page.meeting, identifier
        self.closed = self.busy = self.sample_active = False
        self._sample_position = None
        self.future = self.retry = None
        self.timer = 0
        self.connect("closed", self._closed)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
            margin_top=24,
            margin_bottom=20,
            margin_start=24,
            margin_end=24,
        )
        self.set_child(content)
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
        self.entry.set_text(value or "")
        self.entry.connect("changed", self._changed)
        self._validate()
        self.set_default_widget(self.save)
        self.timer = GLib.timeout_add(50, self._tick)

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

    def _changed(self, *_):
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
        self.save.set_sensitive(not self.busy and (valid or self.retry is not None))

    def _save(self, *_):
        if self.retry is not None:
            operation = self.retry
        else:
            value = self.entry.get_text()

            def operation():
                return speaker_edits.save_name(self.meeting.directory, self.identifier, value)

        self._submit(operation)

    def _merge(self, target):
        self._submit(lambda: speaker_edits.merge_into(self.meeting.directory, self.identifier, target))

    def _submit(self, operation):
        if self.closed or self.busy or self.window.monitor._closed:
            return
        self._finish_sample()
        self.busy = True
        self.error.set_visible(False)
        self.save.set_sensitive(False)
        self.merge.set_sensitive(False)
        self.entry.set_sensitive(False)

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
        except (OSError, ValueError):
            LOG.exception("Speaker edit failed; keeping names and merge history")
            if not self.closed:
                self.retry = operation
                self.error.set_visible(True)
                self.save.set_label(text("common.tryAgain"))
                self.entry.set_sensitive(True)
                self.update(self.meeting)
                self._validate()
        else:
            if not self.window.monitor._closed:
                self.window.monitor.refresh_now()
            self.close()
        return False

    def _can_play(self):
        return bool(
            self.meeting.has_audio
            and self.page.player.loaded
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
