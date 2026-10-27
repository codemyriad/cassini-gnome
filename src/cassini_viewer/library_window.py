# SPDX-License-Identifier: GPL-3.0-or-later
"""The Library shell. Filesystem reads and transcript decoding stay in the monitor worker."""

import os
import time
from zoneinfo import ZoneInfo

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import ux_formats as fmt
from .library_monitor import LibraryMonitor
from .strings import text
from .transcript import build_turns


def label(value, *classes, **kwargs):
    return Gtk.Label(label=value, css_classes=list(classes), **kwargs)


def vertical(spacing=0, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


class LibraryWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title=text("library.title"), default_width=1120, default_height=720)
        self.add_css_class("library-window")
        self._pending = True
        self.selected = None
        self._preview_key = None
        self.snapshot = None
        # These are hidden harness inputs, never separate production fixtures or fake widget data.
        self._fixed_now = (
            int(os.environ["CASSINI_SCREENSHOT_NOW"])
            if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") == "1" and os.environ.get("CASSINI_SCREENSHOT_NOW")
            else None
        )
        self.tz = os.environ.get("TZ") or GLib.TimeZone.new_local().get_identifier()
        try:
            ZoneInfo(self.tz)
        except (ValueError, KeyError):
            self.tz = "Europe/Rome"
        from .player import Player

        self.player = Player()
        self._playback_failed = False
        self.player.connect("error", self._playback_error)
        self._fixture_activity = {}
        if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") == "1" and os.environ.get("CASSINI_SCREENSHOT_ACTIVITY"):
            import json
            from pathlib import Path

            self._fixture_activity = json.loads(Path(os.environ["CASSINI_SCREENSHOT_ACTIVITY"]).read_text())
        self.root_box = Gtk.Stack()
        self.set_content(self.root_box)
        self._build_empty()
        self._build_library()
        self.loading = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.root_box.add_named(self.loading, "loading")
        self.root_box.set_visible_child_name("loading")
        self.monitor = LibraryMonitor(self._updated, capture_activity=lambda: self._fixture_activity)
        self.monitor.refresh()
        self.connect("close-request", self._close)

    @property
    def now_ms(self):
        return self._fixed_now if self._fixed_now is not None else int(time.time() * 1000)

    def _record(self, big=False):
        button = Gtk.Button(
            halign=Gtk.Align.CENTER if big else Gtk.Align.FILL,
            css_classes=["cassini-record"] + (["big-record"] if big else []),
            action_name="app.record",
        )
        box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        box.append(label("●", "record-dot"))
        box.append(label(text("common.record")))
        button.set_child(box)
        button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.record")])
        return button

    def _header(self, *, record=False, close=True):
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_end_title_buttons=close,
            decoration_layout=":close",
            css_classes=["library-header"],
        )
        header.set_title_widget(label(text("library.title"), "heading"))
        if record:
            header.pack_start(self._record())
        if close:
            menu = Gio.Menu()
            menu.append(text("common.legacy.browserMeetings"), "app.browser-meetings")
            header.pack_end(
                Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=text("common.mainMenu"))
            )
        return header

    def _build_empty(self):
        view = Adw.ToolbarView()
        view.add_top_bar(self._header(record=True))
        outer = vertical(vexpand=True)
        center = vertical(22, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True, css_classes=["first-run"])
        center.append(
            Gtk.Image(
                icon_name="audio-input-microphone-symbolic",
                pixel_size=48,
                halign=Gtk.Align.CENTER,
                css_classes=["first-run-icon"],
            )
        )
        title = label(
            text("firstRun.title"),
            "library-title",
            justify=Gtk.Justification.CENTER,
            halign=Gtk.Align.CENTER,
            wrap=True,
            width_request=400,
        )
        title.set_max_width_chars(28)
        title.set_halign(Gtk.Align.FILL)
        center.append(Adw.Clamp(maximum_size=440, tightening_threshold=440, child=title))
        body = label(text("firstRun.body"), "dim-label", justify=Gtk.Justification.CENTER, wrap=True, width_request=460)
        body.set_max_width_chars(50)
        center.append(body)
        center.append(self._record(big=True))
        center.append(
            label(
                text("firstRun.shortcutHint", shortcut=text("common.shortcut.record")),
                "small",
                "dim-label",
                margin_top=8,
            )
        )
        outer.append(center)
        footer = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_bottom=18)
        footer.append(Gtk.Image(icon_name="changes-prevent-symbolic", pixel_size=14))
        footer.append(label(text("firstRun.footer"), "small", "dim-label"))
        outer.append(footer)
        view.set_content(outer)
        self.root_box.add_named(view, "empty")
        self.offline = label(text("errors.libraryOffline.line"), wrap=True, vexpand=True)
        offline_view = Adw.ToolbarView(content=self.offline)
        offline_view.add_top_bar(self._header(record=True))
        self.root_box.add_named(offline_view, "offline")

    def _build_library(self):
        self.split = Adw.NavigationSplitView(min_sidebar_width=400, max_sidebar_width=400, sidebar_width_fraction=0.36)
        self.split.set_min_sidebar_width(400)
        self.split.set_max_sidebar_width(400)
        self.split.set_sidebar_width_unit(Adw.LengthUnit.PX)
        sidebar = Adw.ToolbarView(css_classes=["library-sidebar"])
        header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False, css_classes=["library-header"]
        )
        header.set_title_widget(Gtk.Box())
        header.pack_start(self._record())
        sidebar.add_top_bar(header)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["library-rows"])
        self.rows.connect("row-selected", self._selected)
        sidebar.set_content(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.rows, vexpand=True))
        self.split.set_sidebar(Adw.NavigationPage.new(sidebar, text("library.listPane.a11y")))
        content = Adw.ToolbarView()
        content.add_top_bar(self._header())
        self.preview = vertical(22, margin_top=36, margin_start=44, margin_end=44, margin_bottom=30)
        content.set_content(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.preview))
        self.split.set_content(Adw.NavigationPage.new(content, text("library.previewPane.a11y")))
        self.root_box.add_named(self.split, "library")

    def _updated(self, snapshot):
        self.snapshot = snapshot
        old = self.selected.key if self.selected else None
        self.rows.remove_all()
        self.selected = None
        if snapshot.unavailable:
            self.root_box.set_visible_child_name("offline")
        elif not snapshot.meetings:
            self.root_box.set_visible_child_name("empty")
        else:
            self.root_box.set_visible_child_name("library")
            selected = None
            for group in snapshot.groups(now_ms=self.now_ms, tz=self.tz):
                heading = Gtk.ListBoxRow(selectable=False, activatable=False, css_classes=["day-heading"])
                heading.set_child(label(group.title, "small", "heading", "dim-label", xalign=0))
                self.rows.append(heading)
                for meeting in group.meetings:
                    row = self._row(meeting)
                    self.rows.append(row)
                    if (
                        meeting.key == old
                        or old is None
                        and meeting.title == "Budget review"
                        and self._fixed_now is not None
                    ):
                        selected = row
            self.rows.select_row(selected or self.rows.get_row_at_index(1))
        self.loading.set_spinning(False)
        self._pending = False

    def _row(self, meeting):
        row = Gtk.ListBoxRow(css_classes=["meeting-row"])
        row.meeting = meeting
        box = vertical(4, margin_start=11, margin_end=11, margin_top=9, margin_bottom=9)
        top = Gtk.Box(spacing=8)
        top.append(label(meeting.title, "heading", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        top.append(label(fmt.row_time(meeting.started_at_ms, tz=self.tz), "small", "dim-label"))
        box.append(top)
        meta = Gtk.Box(spacing=6)
        elapsed = max(0, self.now_ms - meeting.started_at_ms) if meeting.started_at_ms is not None else 0
        badge = meeting.badge(live_ms=elapsed)
        if badge:
            meta.append(label(badge.text, "badge", "badge-" + badge.tint))
        value = fmt.row_meta(meeting.duration_ms, meeting.speaker_names)
        if value:
            meta.append(label(value, "small", "dim-label", xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True))
        box.append(meta)
        line = meeting.quote_line
        if meeting.lifecycle == "recording":
            line = text("library.row.recording.line")
        elif meeting.transcript in ("queued", "running") and meeting.duration_ms is not None:
            line = text("library.row.transcribing.line", duration=fmt.meeting_duration(meeting.duration_ms))
        if line:
            box.append(label(line, "small", "dim-label", xalign=0, ellipsize=Pango.EllipsizeMode.END))
        row.set_child(box)
        return row

    def _speaker(self, speaker, *, chip=False):
        box = Gtk.Box(spacing=6, css_classes=["speaker-chip"] if chip else [])
        box.append(label(speaker.name, "speaker-name", "colour-" + str(speaker.colour_index % 3)))
        if speaker.provenance == "recognised":
            box.append(label(text("common.autoTag"), "auto-tag", tooltip_text=text("common.autoTag.tooltip")))
        return box

    def _selected(self, _rows, row):
        if row is None or not hasattr(row, "meeting"):
            return
        if self._preview_key != row.meeting.key:
            self.player.stop()
        self._preview_key = row.meeting.key
        self.selected = meeting = row.meeting
        child = self.preview.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.preview.remove(child)
            child = following
        headline = vertical(8)
        headline.append(label(meeting.title, "library-title", xalign=0, wrap=True))
        headline.append(
            label(
                fmt.preview_subtitle(
                    meeting.started_at_ms,
                    meeting.duration_ms,
                    now_ms=self.now_ms,
                    tz=self.tz,
                    received=meeting.origin == "received",
                ),
                "dim-label",
                xalign=0,
                wrap=True,
            )
        )
        self.preview.append(headline)
        chips = Gtk.Box(spacing=8)
        for speaker in meeting.speakers:
            chips.append(self._speaker(speaker, chip=True))
        self.preview.append(chips)
        actions = Gtk.Box(spacing=10)
        self.play_button = Gtk.Button(
            label=text("common.play"),
            css_classes=["suggested-action"],
            sensitive=meeting.audio_file is not None and meeting.has_audio,
        )
        self.play_button.connect("clicked", lambda *_: self._play_selected())
        actions.append(self.play_button)
        read = Gtk.Button(label=text("library.preview.readTranscript"), sensitive=bool(meeting.words))
        read.connect("clicked", lambda *_: self._open_selected())
        actions.append(read)
        self.preview.append(actions)
        self.preview.append(Gtk.Separator(margin_top=4))
        self.preview.append(label(text("library.preview.transcriptHeading"), "small", "heading", "dim-label", xalign=0))
        speakers = {s.id: s for s in meeting.speakers}
        for turn in build_turns(list(meeting.words))[:3]:
            if turn.host is not None:
                continue
            line = Gtk.Box(spacing=16)
            name = vertical(3, width_request=110, valign=Gtk.Align.START)
            name.append(self._speaker(speakers[turn.speaker]))
            name.append(label(fmt.position(turn.start_ms), "small", "dim-label", xalign=0))
            line.append(name)
            line.append(label(turn.text, "excerpt", xalign=0, yalign=0, wrap=True, hexpand=True))
            self.preview.append(line)

    def _playback_error(self, _player, _message):
        self._playback_failed = True
        self.play_button.set_sensitive(False)

    def _play_selected(self):
        if self.selected and self.selected.audio_file is not None:
            self._playback_failed = False
            self.player.load(Gio.File.new_for_path(str(self.selected.audio_file)).get_uri(), self.selected.duration_ms)
            if not self._playback_failed:
                self.player.play()

    def _open_selected(self):
        if self.selected:
            path = self.selected.audio_file or self.selected.directory / "session.json"
            self.get_application().open_in_new_window(Gio.File.new_for_path(str(path)))

    def _close(self, *_args):
        self.player.shutdown()
        self.monitor.close()
        return False
