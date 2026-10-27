# SPDX-License-Identifier: GPL-3.0-or-later
"""The Library shell. Filesystem reads and transcript decoding stay in the monitor worker."""

import os
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import ux_formats as fmt
from .capture_activity import BrowserActivity, recorder_activity
from .folder_player import FolderPlayer
from .library_monitor import LibraryMonitor
from .player import Player
from .strings import text


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
        self.play_button = None
        self.read_button = None
        self._preview_key = None
        self._loaded_key = None
        self.snapshot = None
        self._list_signature = None
        self._preview_signature_value = None
        self._live_badges = {}
        self._activity_source = None
        self._activity_timer = 0
        # These are hidden harness inputs, never separate production fixtures or fake widget data.
        self._fixed_now = (
            int(os.environ["CASSINI_SCREENSHOT_NOW"])
            if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") == "1" and os.environ.get("CASSINI_SCREENSHOT_NOW")
            else None
        )
        self.tz = fmt.local_timezone(os.environ.get("TZ") or GLib.TimeZone.new_local().get_identifier())
        self.player = Player()
        self._playback_failed = False
        self._connect_player()
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
        self._activity = dict(self._fixture_activity)
        self.monitor = LibraryMonitor(self._updated, capture_activity=lambda: self._activity)
        if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1":
            self._activity_source = BrowserActivity(self._activity_tick)
            self._activity_timer = GLib.timeout_add_seconds(1, self._activity_tick)
        self.monitor.refresh()
        self.connect("close-request", self._close)
        for name, callback in (
            ("play-pause", self._play_selected),
            ("skip-back", lambda *_: self.player.skip(-10000)),
            ("skip-forward", lambda *_: self.player.skip(10000)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param, call=callback: call())
            self.add_action(action)

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
        box.append(label("●", "record-dot", accessible_role=Gtk.AccessibleRole.PRESENTATION))
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
            button = Gtk.MenuButton(
                icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=text("common.mainMenu"), primary=True
            )
            button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.mainMenu")])
            header.pack_end(button)
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
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
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
        footer.append(
            Gtk.Image(
                icon_name="changes-prevent-symbolic", pixel_size=14, accessible_role=Gtk.AccessibleRole.PRESENTATION
            )
        )
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
        self.capture_header = Gtk.Stack()
        self.capture_header.add_named(self._record(), "record")
        live = Gtk.Box(spacing=8, css_classes=["live-pill"], valign=Gtk.Align.CENTER)
        live.append(Gtk.Box(width_request=10, height_request=10, valign=Gtk.Align.CENTER, css_classes=["live-dot"]))
        self.live_timer = label("", "live-timer")
        live.append(self.live_timer)
        levels = vertical(3, valign=Gtk.Align.CENTER, css_classes=["live-levels"])
        for fraction in (0.64, 0.38):
            levels.append(Gtk.ProgressBar(fraction=fraction, width_request=30))
        live.append(levels)
        self.live_stop = Gtk.Button(label=text("common.stop"), sensitive=False, css_classes=["static-stop"])
        live.append(self.live_stop)
        self.capture_header.add_named(live, "live")
        header.pack_start(self.capture_header)
        sidebar.add_top_bar(header)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, css_classes=["library-rows"])
        self._selection_handler = self.rows.connect("row-selected", self._selected)
        sidebar.set_content(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.rows, vexpand=True))
        self.split.set_sidebar(Adw.NavigationPage.new(sidebar, text("library.listPane.a11y")))
        content = Adw.ToolbarView()
        content.add_top_bar(self._header())
        self.preview = vertical(22, margin_top=36, margin_start=44, margin_end=44, margin_bottom=30)
        content.set_content(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.preview))
        self.split.set_content(Adw.NavigationPage.new(content, text("library.previewPane.a11y")))
        self.root_box.add_named(self.split, "library")

    def _activity_tick(self):
        activity = dict(self._activity_source.snapshot) if self._activity_source else {}
        activity.update(recorder_activity(self.get_application().get_windows()))
        if activity != self._activity:
            self._activity = activity
            self.monitor.refresh()
        self._update_live()
        # Re-group at local midnight without touching the filesystem.
        if self.snapshot is not None:
            self._updated(self.snapshot)
        return GLib.SOURCE_CONTINUE

    def _update_live(self):
        if self.snapshot is None:
            return
        live = next((m for m in self.snapshot.meetings if m.lifecycle == "recording"), None)
        self.capture_header.set_visible_child_name("live" if live else "record")
        for meeting in self.snapshot.meetings:
            if meeting.lifecycle == "recording":
                elapsed = max(0, self.now_ms - meeting.started_at_ms) if meeting.started_at_ms is not None else 0
                if meeting is live:
                    self.live_timer.set_label(fmt.live_badge(elapsed))
                if meeting.key in self._live_badges:
                    self._live_badges[meeting.key].set_label(meeting.badge(live_ms=elapsed).text)

    def _preview_signature(self, meeting):
        return (
            meeting.key,
            meeting.title,
            fmt.preview_subtitle(
                meeting.started_at_ms,
                meeting.duration_ms,
                now_ms=self.now_ms,
                tz=self.tz,
                received=meeting.origin == "received",
            ),
            meeting.speakers,
            meeting.preview_turns,
            meeting.has_audio,
            meeting.legacy_transcript_file,
            meeting.lifecycle,
            tuple(t.start_offset_ms for t in meeting.tracks),
        )

    def _signature(self, snapshot):
        selected = self.selected.key if self.selected else None
        return (
            snapshot.unavailable,
            tuple(
                (
                    group.title,
                    tuple(
                        (
                            m.key,
                            m.title,
                            fmt.row_time(m.started_at_ms, tz=self.tz),
                            m.badge().kind if m.badge() else None,
                            m.badge().text if m.badge() and m.lifecycle != "recording" else None,
                            m.row_texts(),
                            self._preview_signature(m) if m.key == selected else None,
                        )
                        for m in group.meetings
                    ),
                )
                for group in snapshot.groups(now_ms=self.now_ms, tz=self.tz)
            ),
        )

    def _updated(self, snapshot):
        self.snapshot = snapshot
        self._update_live()
        if self._signature(snapshot) == self._list_signature:
            current = {m.key: m for m in snapshot.meetings}
            row = self.rows.get_first_child()
            while row:
                if hasattr(row, "meeting"):
                    row.meeting = current[row.meeting.key]
                row = row.get_next_sibling()
            if self.selected:
                self.selected = current.get(self.selected.key)
            self._pending = False
            return
        old = self.selected.key if self.selected else None
        self.rows.handler_block(self._selection_handler)
        self.rows.remove_all()
        self._live_badges = {}
        self.selected = None
        if snapshot.unavailable:
            self._stop_playback(clear_selection=True)
            self.root_box.set_visible_child_name("offline")
        elif not snapshot.meetings:
            self._stop_playback(clear_selection=True)
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
        self.rows.handler_unblock(self._selection_handler)
        self._selected(self.rows, self.rows.get_selected_row())
        self._list_signature = self._signature(snapshot)
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
            badge_label = label(badge.text, "badge", "badge-" + badge.tint)
            meta.append(badge_label)
            if meeting.lifecycle == "recording":
                self._live_badges[meeting.key] = badge_label
        value, line = meeting.row_texts()
        if value:
            meta.append(label(value, "small", "dim-label", xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True))
        box.append(meta)
        if line:
            box.append(label(line, "small", "dim-label", xalign=0, ellipsize=Pango.EllipsizeMode.END))
        row.set_child(box)
        return row

    def _speaker(self, speaker, *, chip=False):
        box = Gtk.Box(spacing=6, css_classes=["speaker-chip"] if chip else [])
        box.append(label(speaker.name, "speaker-name"))
        if speaker.provenance == "recognised":
            tag = label(text("common.autoTag"), "auto-tag", tooltip_text=text("common.autoTag.tooltip"))
            tag.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text("common.autoTag.tooltip")])
            box.append(tag)
        return box

    def _selected(self, _rows, row):
        if row is None or not hasattr(row, "meeting"):
            return
        if self._preview_key != row.meeting.key:
            self._stop_playback()
        elif not row.meeting.has_audio:
            self.player.stop()
            self._loaded_key = None
        self._preview_key = row.meeting.key
        self.selected = meeting = row.meeting
        self._list_signature = self._signature(self.snapshot)
        signature = self._preview_signature(meeting)
        if signature == self._preview_signature_value:
            return
        self._preview_signature_value = signature
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
            label=text("player.pause" if self.player.playing else "common.play"),
            css_classes=["suggested-action"],
            sensitive=meeting.has_audio
            and not self._playback_failed
            and (meeting.audio_file is not None or FolderPlayer.can_play(meeting)),
        )
        self.play_button.connect("clicked", lambda *_: self._play_selected())
        actions.append(self.play_button)
        self.read_button = Gtk.Button(
            label=text("library.preview.readTranscript"), sensitive=meeting.legacy_transcript_file is not None
        )
        self.read_button.connect("clicked", lambda *_: self._open_selected())
        actions.append(self.read_button)
        self.preview.append(actions)
        self.preview.append(Gtk.Separator(margin_top=4))
        self.preview.append(label(text("library.preview.transcriptHeading"), "small", "heading", "dim-label", xalign=0))
        speakers = {s.id: s for s in meeting.speakers}
        for turn in meeting.preview_turns:
            line = Gtk.Box(spacing=16)
            name = vertical(3, width_request=110, valign=Gtk.Align.START)
            name.append(self._speaker(speakers[turn.speaker]))
            name.append(label(fmt.position(turn.start_ms), "small", "dim-label", xalign=0))
            line.append(name)
            line.append(label(turn.text, "excerpt", xalign=0, yalign=0, wrap=True, hexpand=True))
            self.preview.append(line)

    def _connect_player(self):
        self.get_application().watch_player(self.player)
        self.player.connect("error", self._playback_error)
        self.player.connect(
            "notify::playing",
            lambda *_: (
                self.play_button.set_label(text("player.pause" if self.player.playing else "common.play"))
                if self.play_button is not None
                else None
            ),
        )

    def _playback_error(self, _player, _message):
        self._playback_failed = True
        self._loaded_key = None
        if self.play_button is not None:
            self.play_button.set_sensitive(False)

    def _play_selected(self):
        meeting = self.selected
        if meeting is None or not self.play_button.get_sensitive():
            return
        if self._loaded_key == meeting.key:
            self.player.toggle()
            return
        desired = FolderPlayer if meeting.origin == "own" else Player
        if type(self.player) is not desired:
            self.player.shutdown()
            self.player = desired()
            self._connect_player()
        self._playback_failed = False
        try:
            if isinstance(self.player, FolderPlayer):
                self.player.load_tracks(meeting.tracks, meeting.duration_ms)
            else:
                self.player.load(Gio.File.new_for_path(str(meeting.audio_file)).get_uri(), meeting.duration_ms)
        except (OSError, ValueError, RuntimeError, GLib.Error) as error:
            self._playback_error(self.player, str(error))
        if not self._playback_failed:
            self._loaded_key = meeting.key
            self.player.play()

    def _stop_playback(self, *, clear_selection=False):
        self.player.stop()
        self._loaded_key = None
        if clear_selection:
            self._preview_key = None
            self._preview_signature_value = None
        self._playback_failed = False

    def _open_selected(self):
        if self.selected and self.selected.legacy_transcript_file is not None:
            self._stop_playback()
            self.get_application().open_in_new_window(Gio.File.new_for_path(str(self.selected.legacy_transcript_file)))

    def _close(self, *_args):
        if self._activity_timer:
            GLib.source_remove(self._activity_timer)
            self._activity_timer = 0
        if self._activity_source is not None:
            self._activity_source.close()
        self.player.shutdown()
        self.monitor.close()
        return False
