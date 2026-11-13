# SPDX-License-Identifier: GPL-3.0-or-later
"""Right-pane and external Meeting view: inline speakers, Find bar, virtualised transcript, and floating player."""

import os
from decimal import Decimal

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango

from . import ux_formats as fmt
from .exact_transcript import ExactTranscript
from .i18n import language, using_language
from .inline_line import InlineLine
from .listening_bar import ListeningBar
from .listening_shortcuts import ListeningShortcuts
from .listening_timeline import ListeningTimeline
from .markup_runs import bridge_link_spaces
from .meeting_text import (
    FollowRows,
    build_find_row_highlights,
    changed_header_rows,
    find_transcript_matches,
    reading_markup,
    reading_markup_signature,
    reading_word,
    row_caption,
)
from .strings import plural, text
from .tokens import speaker_colour, speaker_slot, token
from .transcribing_status import TranscribingStatusRow
from .transcript_label import TranscriptLabel
from .transcript_row import ExactTurnRow


def box(spacing=0, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def label(value, *classes, **kwargs):
    return Gtk.Label(label=value, css_classes=list(classes), **kwargs)


class _ScrollFade(Gtk.Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_size_request(-1, 40)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        top = Gdk.RGBA()
        top.parse(token("surface", dark=Adw.StyleManager.get_default().get_dark()))
        bottom = Gdk.RGBA()
        bottom.red, bottom.green, bottom.blue, bottom.alpha = top.red, top.green, top.blue, 0.0
        s0 = Gsk.ColorStop()
        s0.offset = 0.0
        s0.color = top
        s1 = Gsk.ColorStop()
        s1.offset = 1.0
        s1.color = bottom
        snapshot.append_linear_gradient(
            Graphene.Rect().init(0, 0, w, h),
            Graphene.Point().init(0, 0),
            Graphene.Point().init(0, h),
            [s0, s1],
        )


class TurnItem(GObject.Object):
    def __init__(self, turn):
        super().__init__()
        self.turn = turn


class MeetingPage(Adw.NavigationPage):
    __gsignals__ = {"rows-rebuilt": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, window, meeting):
        super().__init__(title=meeting.title)
        self.window, self.meeting = window, meeting
        self.speakers_dialog = None
        self._speaker_future = None
        self._merge_toast = None
        self._selected_speaker = None
        self._closed = False
        self.player = window.player
        self._updating = False
        self.bound = {}
        self._signature = None
        self._reading_index = -1
        self._follow_rows = FollowRows(())
        self._turns = ()
        self._header_signature = None
        self._header_source = 0
        self._header_pending = []
        self._listening_turns = None
        self._updating_rows = False
        self._updating_transcript = False
        self._speaker_rows = {}
        self._rows_rebuilt_tick = 0
        self._can_listen = False
        self._listening_target = None
        self._scroll_target = None
        self._highlighted_rows = frozenset()
        self._turn_rows = {}
        self._row_listening = {}
        self._local_turn_index = {}
        self._follow_until = 0
        self._highlight_target = None
        self._header_scrolled = False
        self._pending_header_scrolled = None
        self._scroll_header_source = 0
        self._last_full_header_h = 104
        self._last_vadj_value = 0.0
        self._find_query = ""
        self._find_matches = ()
        self._find_index = -1
        self._find_row_highlights = {}
        self._find_focused = False
        self.name_speakers_button = None
        self.settings = Gio.Settings.new(window.get_application().settings_schema)

        outer = box(hexpand=True, vexpand=True, css_classes=["meeting-pane"])
        self.set_child(outer)

        self.headline = Adw.WindowTitle()
        self.top_bar = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            show_end_title_buttons=True,
            decoration_layout=":close",
            show_title=False,
            css_classes=["meeting-topbar"],
        )
        self.back_button = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text=text("common.backToLibrary"),
            css_classes=["flat", "meeting-icon-btn"],
            action_name="win.back-library",
            visible=False,
        )
        self.back_button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.backToLibrary")])
        self.top_bar.pack_start(self.back_button)

        self.compact_header_box = Gtk.Box(spacing=8, hexpand=True, valign=Gtk.Align.CENTER, visible=False, margin_end=8)
        self.compact_title = label("", "meeting-compact-title", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.compact_subtitle = label("", "meeting-compact-subtitle", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.compact_header_box.append(self.compact_title)
        self.compact_header_box.append(self.compact_subtitle)
        self.top_bar.pack_start(self.compact_header_box)

        actions_box = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        self.write_button = Gtk.Button(
            label=text("common.writeDown"),
            css_classes=["suggested-action"],
            valign=Gtk.Align.CENTER,
            visible=False,
        )
        self.write_button.connect("clicked", lambda *_: self.window.get_application().write_down(self.meeting))
        actions_box.append(self.write_button)

        export_content = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        export_content.append(Gtk.Label(label=text("export.exportButton")))
        export_content.append(
            Gtk.Image(
                icon_name="pan-down-symbolic",
                pixel_size=11,
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        self.export_button = Gtk.Button(
            child=export_content,
            tooltip_text=text("common.exportEllipsis"),
            action_name="win.export",
            css_classes=["flat", "meeting-export-btn"],
            valign=Gtk.Align.CENTER,
        )
        self.export_button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.exportEllipsis")])
        actions_box.append(self.export_button)

        self.trash_button = Gtk.Button(
            icon_name="user-trash-symbolic",
            tooltip_text=text("common.moveToTrash"),
            action_name="win.move-to-trash",
            css_classes=["flat", "meeting-icon-btn"],
            valign=Gtk.Align.CENTER,
        )
        self.trash_button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.moveToTrash")])
        actions_box.append(self.trash_button)

        menu = self.more_menu = Gio.Menu()
        menu.append(text("details.menu"), "win.recording-details")
        menu.append(text("common.exportEllipsis"), "win.export")
        self.more_button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text=text("meeting.moreActions"),
            menu_model=menu,
            css_classes=["flat", "meeting-icon-btn"],
            valign=Gtk.Align.CENTER,
        )
        self.more_button.get_popover().set_halign(Gtk.Align.END)
        self.more_button.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.moreActions")])
        actions_box.append(self.more_button)
        self.top_bar.pack_end(actions_box)
        outer.append(self.top_bar)

        self.full_header = box(6, css_classes=["meeting-full-header"])
        title_row = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.title_label = label(
            "",
            "meeting-title",
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        title_row.append(self.title_label)
        self.rename_button = Gtk.Button(
            icon_name="document-edit-symbolic",
            tooltip_text=text("meeting.rename.confirm"),
            action_name="win.rename",
            css_classes=["flat", "circular", "meeting-rename-btn"],
            valign=Gtk.Align.CENTER,
        )
        self.rename_button.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.rename.confirm")])
        title_row.append(self.rename_button)
        self.full_header.append(title_row)

        self.subtitle_label = label("", "meeting-subtitle", xalign=0, wrap=True)
        self.full_header.append(self.subtitle_label)

        self.speakers = Adw.WrapBox(child_spacing=16, line_spacing=8, margin_top=2)
        self.speakers.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.speakersPane.a11y")])
        self.speakers_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True,
            max_content_height=68,
            child=self.speakers,
        )
        self.speakers_scroller.get_child().set_scroll_to_focus(False)
        self.full_header.append(self.speakers_scroller)
        self.transcribing_row = TranscribingStatusRow(
            on_stop=lambda: self.window.get_application().stop_transcription(self.meeting)
        )
        self.full_header.append(self.transcribing_row)
        outer.append(self.full_header)

        self.stop_transcription_button = None
        self.reading_surface = box(vexpand=True, hexpand=True, css_classes=["meeting-reading-surface"])
        self.recording_notice = box(margin_start=48, margin_end=48, margin_top=8, visible=False)
        self._notice_signature = None
        self.reading_surface.append(self.recording_notice)
        self.lines = box(margin_start=48, margin_end=48, margin_top=8, visible=False)
        self.reading_surface.append(self.lines)
        self._companion = self._companion_card()
        self._companion.set_margin_start(48)
        self._companion.set_margin_end(48)
        self._companion.set_margin_top(8)
        self.reading_surface.append(self._companion)

        self._build_find_bar(self.reading_surface)

        self.store = Gio.ListStore.new(TurnItem)
        self._exact = os.environ.get("CASSINI_TRANSCRIPT_RENDER") != "listview"
        if self._exact:
            self.list = ExactTranscript(self, bottom_inset=72)
        else:
            factory = Gtk.SignalListItemFactory()
            factory.connect("bind", self._bind)
            factory.connect("unbind", self._unbind)
            self.list = Gtk.ListView(
                model=Gtk.NoSelection.new(self.store), factory=factory, css_classes=["meeting-transcript"]
            )
            self.list.set_enable_rubberband(False)
        shortcut = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        for key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            shortcut.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.KeyvalTrigger.new(key, Gdk.ModifierType.SHIFT_MASK),
                    Gtk.CallbackAction.new(lambda *_: self._play_focused_row()),
                )
            )
        self.list.add_controller(shortcut)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(self.list)
        self._vadj = self.scroller.get_vadjustment()
        self._vadj_handler = self._vadj.connect("value-changed", self._on_vadj_changed)

        self.transcript_overlay = Gtk.Overlay(vexpand=True, hexpand=True)
        self.transcript_overlay.set_child(self.scroller)
        self.transcribing_blank = Gtk.Box(
            vexpand=True,
            hexpand=True,
            css_classes=["meeting-reading-surface"],
            visible=False,
        )
        self.transcript_overlay.add_overlay(self.transcribing_blank)
        self.transcript_overlay.set_measure_overlay(self.transcribing_blank, False)
        self.scroll_fade = _ScrollFade(
            css_classes=["meeting-scroll-fade"],
            valign=Gtk.Align.START,
            hexpand=True,
            can_target=False,
            visible=False,
        )
        self.transcript_overlay.add_overlay(self.scroll_fade)
        self.transcript_overlay.set_measure_overlay(self.scroll_fade, False)
        self._build_player(self.transcript_overlay)
        self.reading_surface.append(self.transcript_overlay)
        outer.append(self.reading_surface)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES, propagation_phase=Gtk.PropagationPhase.CAPTURE
        )
        scroll.connect("scroll", self._hand_scroll)
        self.scroller.add_controller(scroll)
        drag = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        for signal in ("drag-begin", "drag-update", "drag-end"):
            drag.connect(signal, self._hand_scroll)
        self.scroller.get_vscrollbar().add_controller(drag)
        # Observe GTK's existing touchscreen gestures without replacing or
        # claiming them. Touch drag/flick does not emit the scroll signal.
        self._touch_scroll_signals = []
        if self._exact:
            controllers = self.scroller.observe_controllers()
            for i in range(controllers.get_n_items()):
                controller = controllers.get_item(i)
                signals = ()
                if isinstance(controller, Gtk.GestureDrag):
                    signals = ("drag-begin", "drag-update", "drag-end")
                elif isinstance(controller, Gtk.GestureSwipe):
                    signals = ("swipe",)
                for signal in signals:
                    self._touch_scroll_signals.append((controller, controller.connect(signal, self._hand_scroll)))

        self._listen_origin = None
        self.speakers.connect("map", self._schedule_rows_ready)
        self._merge_transition = None
        self._dialog_focus = None
        self._dialog_focus_idle = 0
        self._dialog_focus_signals = []
        self.listening_bar = ListeningBar(self)
        self.listening_bar.connect("notify::child-revealed", self._listening_revealed)
        outer.append(self.listening_bar)

        self._split_collapsed_handler = None
        if hasattr(window, "split") and window.split is not None:
            self._split_collapsed_handler = window.split.connect("notify::collapsed", self._sync_back_button)
        self._sync_back_button()

        self._style_manager = Adw.StyleManager.get_default()
        self._style_source = 0
        self._style_signals = [
            self._style_manager.connect("notify::" + name, self._style_changed) for name in ("dark", "high-contrast")
        ]
        self._signals = []
        self.rebind_player(self.player)
        self._listen_signals = [
            window.listen.connect(signal, self._listening_changed) for signal in ("changed", "stopped")
        ]
        self._listen_signals.append(window.listen.connect("requested", self._listening_requested))
        self.listening_keys = ListeningShortcuts(self)
        self.update(meeting)
        self._position()

    def _sync_back_button(self, *_):
        collapsed = bool(hasattr(self.window, "split") and self.window.split and self.window.split.get_collapsed())
        self.back_button.set_visible(bool(self.window._external_meeting or collapsed))

    def rebind_player(self, player):
        if getattr(self, "_signals", None) and self.player is not None:
            for handler in self._signals:
                self.player.disconnect(handler)
            self._signals.clear()
        self.player = player
        self._signals = [
            self.player.connect("notify::" + name, self._position) for name in ("position", "playing", "duration")
        ]
        self._signals.append(self.player.connect("error", self._error))
        if hasattr(self, "elapsed") and not self._closed:
            self._position()

    def select_meeting(self, meeting, *, reset_scroll=False):
        changed_key = meeting.key != self.meeting.key
        if changed_key or reset_scroll:
            self._set_header_scrolled(False)
            self._last_vadj_value = 0.0
            self._vadj.set_value(0)
        if changed_key:
            self.transcribing_row.popdown()
            self.clear_find(focus_transcript=False)
            self._clear_dialog_focus()
            self._speaker_future = None
            if getattr(self, "_merge_toast", None) is not None:
                self._merge_toast.dismiss()
                self._merge_toast = None
            self._reading_index = -1
            self._follow_until = 0
            self._selected_speaker = None
            self._merge_transition = None
            self._header_signature = None
            self._signature = None
            self.set_recording_notice(None, 0)
            if self.speakers_dialog and not self.speakers_dialog.closed:
                self.speakers_dialog.close()
        self.set_title(meeting.title)
        self.update(meeting)
        self._position()

    def _on_vadj_changed(self, adj):
        if self._closed or self._updating_transcript or self.meeting.transcript in ("queued", "running"):
            return
        if self.full_header.get_visible():
            measured = self.full_header.get_height()
            if measured > 0:
                self._last_full_header_h = measured
        value = adj.get_value()
        prev = self._last_vadj_value
        self._last_vadj_value = value
        upper = adj.get_upper()
        page_size = adj.get_page_size()
        if not self._header_scrolled:
            if (
                self.get_mapped()
                and not self.window.listen.active
                and value > prev + 0.5
                and value >= 48
                and (upper - (page_size + max(self._last_full_header_h, 96))) >= 48
            ):
                self._queue_header_scrolled(True)
        elif value <= 2:
            self._queue_header_scrolled(False)

    def _queue_header_scrolled(self, scrolled):
        # vadjustment::value-changed can fire inside ExactTranscript.do_size_allocate;
        # deferring set_visible avoids mid-allocation GTK measurement warnings.
        self._pending_header_scrolled = bool(scrolled)
        if not self._scroll_header_source:
            self._scroll_header_source = GLib.idle_add(self._flush_header_scrolled)

    def _flush_header_scrolled(self):
        self._scroll_header_source = 0
        pending = self._pending_header_scrolled
        self._pending_header_scrolled = None
        if not self._closed and pending is not None:
            self._set_header_scrolled(pending)
        return GLib.SOURCE_REMOVE

    def _set_header_scrolled(self, scrolled):
        if getattr(self, "_scroll_header_source", 0):
            GLib.source_remove(self._scroll_header_source)
            self._scroll_header_source = 0
        self._pending_header_scrolled = None
        scrolled = bool(scrolled)
        if self._header_scrolled == scrolled:
            return
        self._header_scrolled = scrolled
        self.full_header.set_visible(not scrolled)
        self.compact_header_box.set_visible(scrolled)
        self.scroll_fade.set_visible(scrolled)
        (self.top_bar.add_css_class if scrolled else self.top_bar.remove_css_class)("scrolled")

    def set_scrolled_header(self, scrolled):
        self._set_header_scrolled(scrolled)

    def _build_find_bar(self, parent):
        self.find_row = Gtk.Box(css_classes=["meeting-find-row"], visible=False)
        self.find_bar = Gtk.Box(
            spacing=7,
            hexpand=True,
            valign=Gtk.Align.CENTER,
            css_classes=["meeting-find-bar"],
        )
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_: self.focus_find())
        self.find_bar.add_controller(click)

        icon = Gtk.Image(
            icon_name="system-search-symbolic",
            pixel_size=14,
            valign=Gtk.Align.CENTER,
            css_classes=["meeting-find-icon"],
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self.find_bar.append(icon)

        self.find_entry = Gtk.Text(
            placeholder_text=text("meeting.find.placeholder"),
            hexpand=True,
            valign=Gtk.Align.CENTER,
            css_classes=["meeting-find-input"],
        )
        self.find_entry.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.find.placeholder")])
        self.window.protect_text_input(self.find_entry)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self._set_find_focused(True))
        focus.connect("leave", lambda *_: self._set_find_focused(False))
        self.find_entry.add_controller(focus)
        self.find_entry.connect("changed", self._on_find_changed)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_find_key)
        self.find_entry.add_controller(keys)
        self.find_bar.append(self.find_entry)

        self.find_shortcut_badge = label("Ctrl+F", "meeting-find-shortcut", valign=Gtk.Align.CENTER)
        self.find_bar.append(self.find_shortcut_badge)

        self.find_count_label = label("", "meeting-find-count", valign=Gtk.Align.CENTER, visible=False)
        self.find_bar.append(self.find_count_label)

        self.find_prev_button = Gtk.Button(
            icon_name="go-up-symbolic",
            tooltip_text=text("mac.legacy.previousMatch"),
            css_classes=["flat", "meeting-find-step"],
            valign=Gtk.Align.CENTER,
            visible=False,
        )
        self.find_prev_button.update_property([Gtk.AccessibleProperty.LABEL], [text("mac.legacy.previousMatch")])
        self.find_prev_button.connect("clicked", lambda *_: self.step_find(-1))
        self.find_bar.append(self.find_prev_button)

        self.find_next_button = Gtk.Button(
            icon_name="go-down-symbolic",
            tooltip_text=text("mac.legacy.nextMatch"),
            css_classes=["flat", "meeting-find-step"],
            valign=Gtk.Align.CENTER,
            visible=False,
        )
        self.find_next_button.update_property([Gtk.AccessibleProperty.LABEL], [text("mac.legacy.nextMatch")])
        self.find_next_button.connect("clicked", lambda *_: self.step_find(1))
        self.find_bar.append(self.find_next_button)

        self.find_row.append(self.find_bar)
        parent.append(self.find_row)

    def _set_find_focused(self, focused):
        self._find_focused = bool(focused)
        (self.find_bar.add_css_class if self._find_focused else self.find_bar.remove_css_class)("focused")
        self._sync_find_chrome()

    def focus_find(self):
        if not self.find_row.get_visible():
            return False
        self.find_entry.grab_focus()
        current = self.find_entry.get_text()
        if current:
            self.find_entry.select_region(0, len(current))
        return True

    def clear_find(self, *, focus_transcript=True):
        had_text = bool(self.find_entry.get_text())
        if had_text:
            self.find_entry.set_text("")
        if focus_transcript and self.bound:
            first_row = next(iter(self.bound.values()))[2]
            if first_row.get_mapped():
                first_row.grab_focus()
                return True
        if focus_transcript and self.play.get_mapped():
            self.play.grab_focus()
        return True

    def _on_find_key(self, _controller, keyval, _keycode, state):
        if keyval == Gdk.KEY_Escape:
            return self.clear_find(focus_transcript=True)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            self.step_find(-1 if state & Gdk.ModifierType.SHIFT_MASK else 1)
            return True
        return False

    def _on_find_changed(self, entry):
        self._find_query = entry.get_text().strip()
        self._recompute_find_matches(scroll=True)

    def _recompute_find_matches(self, *, scroll=False):
        if not self._find_query:
            changed = bool(self._find_row_highlights or self._find_matches)
            self._find_matches = ()
            self._find_index = -1
            self._find_row_highlights = {}
            self._sync_find_chrome()
            if changed:
                self._refresh_find_highlights()
            return
        self._find_matches = find_transcript_matches(self._turns, self._find_query)
        if not self._find_matches:
            self._find_index = -1
            self._find_row_highlights = {}
        else:
            if self._find_index < 0 or self._find_index >= len(self._find_matches):
                self._find_index = 0
            self._find_row_highlights = build_find_row_highlights(self._find_matches, self._find_index)
        self._sync_find_chrome()
        self._refresh_find_highlights()
        if scroll and self._find_matches and 0 <= self._find_index < len(self._find_matches):
            self._scroll_to_find_match()

    def step_find(self, delta):
        if not self._find_matches:
            return False
        self._find_index = (self._find_index + delta) % len(self._find_matches)
        self._find_row_highlights = build_find_row_highlights(self._find_matches, self._find_index)
        self._sync_find_chrome()
        self._refresh_find_highlights()
        self._scroll_to_find_match()
        return True

    def _scroll_to_find_match(self):
        if not self._find_matches or not (0 <= self._find_index < len(self._find_matches)):
            return
        match = self._find_matches[self._find_index]
        self._follow_until = GLib.get_monotonic_time() + 4_000_000
        self.list.scroll_to(match.row_index, Gtk.ListScrollFlags.NONE, None)

    def _refresh_find_highlights(self):
        for turn, words, _row in list(self.bound.values()):
            self._render_words(turn, words)

    def _sync_find_chrome(self):
        has_query = bool(self._find_query)
        count = len(self._find_matches)
        self.find_shortcut_badge.set_visible(not has_query and not self._find_focused)
        self.find_count_label.set_visible(has_query)
        if has_query:
            done = (self._find_index + 1) if count else 0
            self.find_count_label.set_label(text("transcribing.stat.audioDoneValue", done=done, total=count))
        self.find_prev_button.set_visible(has_query)
        self.find_next_button.set_visible(has_query)
        can_step = count > 1
        self.find_prev_button.set_sensitive(can_step)
        self.find_next_button.set_sensitive(can_step)

    def _style_changed(self, _manager=None, property=None):
        if self._closed:
            return
        if fade := getattr(self, "scroll_fade", None):
            fade.queue_draw()
        if property is not None and property.name == "dark":
            # Preserve the existing synchronous header/navigation update.
            self.update(self.meeting)
        # Libadwaita notifies before replacing its stylesheet. Resolve only
        # word colours afterwards, including when a paused page stays bound.
        if not self._style_source:
            self._style_source = GLib.idle_add(self._style_ready)

    def _style_ready(self):
        self._style_source = 0
        if not self._closed:
            if fade := getattr(self, "scroll_fade", None):
                fade.queue_draw()
            if transcribing := getattr(self, "transcribing_row", None):
                transcribing.invalidate_theme()
            if self._exact:
                self.list.invalidate()
            for turn, words, _row in list(self.bound.values()):
                self._render_words(turn, words)
        return GLib.SOURCE_REMOVE

    def _clear_style_refresh(self):
        if self._style_source:
            GLib.source_remove(self._style_source)
            self._style_source = 0
        for handler in self._style_signals:
            self._style_manager.disconnect(handler)
        self._style_signals.clear()

    def _companion_card(self):
        card = Gtk.Box(spacing=16, css_classes=["companion-card"])
        card.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.companionCard.a11y")])
        card.append(
            Gtk.Image(
                icon_name="web-browser-symbolic",
                valign=Gtk.Align.START,
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        body = box(8, hexpand=True)
        body.append(label(text("meeting.companionCard.title"), "heading", xalign=0, wrap=True))
        body.append(label(text("meeting.companionCard.body"), xalign=0, wrap=True))
        buttons = Gtk.Box(spacing=12)
        setup = self.companion_setup_button = Gtk.Button(
            label=text("meeting.companionCard.setUp"), css_classes=["suggested-action"]
        )
        setup.connect("clicked", lambda *_: self.window.get_application().on_companion_setup(parent=self.window))
        later = Gtk.Button(label=text("common.notNow"), css_classes=["flat"])
        later.connect("clicked", self._dismiss_card)
        buttons.append(setup)
        buttons.append(later)
        body.append(buttons)
        card.append(body)
        return card

    def _dismiss_card(self, *_):
        self.settings.set_boolean("companion-card-dismissed", True)
        self.set_connected(False)

    def set_connected(self, connected):
        visible = (
            self.meeting.transcript not in ("queued", "running")
            and self.window.get_application().browser_companion_available
            and not connected
            and not self.settings.get_boolean("companion-card-dismissed")
        )
        self._companion.set_visible(visible)

    def _build_player(self, overlay):
        bar = Gtk.Box(
            spacing=12,
            css_classes=["meeting-player"],
            margin_start=32,
            margin_end=32,
            margin_bottom=16,
            valign=Gtk.Align.END,
        )
        self.play = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            tooltip_text=text("common.play"),
            action_name="win.play-pause",
            css_classes=["circular", "suggested-action"],
            valign=Gtk.Align.CENTER,
        )
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [text("common.play")])
        self.transport_buttons = [self.play]
        bar.append(self.play)

        self.elapsed = label(fmt.position(0), "player-time", valign=Gtk.Align.CENTER)
        bar.append(self.elapsed)

        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(1, self.meeting.duration_ms or 1), 100)
        self.seek.set_draw_value(False)
        self.seek.set_hexpand(True)
        self.seek.set_valign(Gtk.Align.CENTER)
        self.seek.update_property([Gtk.AccessibleProperty.LABEL], [text("player.a11y")])
        self.seek.connect("change-value", self._seek)
        self.timeline = ListeningTimeline(self, self.seek)
        bar.append(self.timeline)

        self.total = label(fmt.position(self.meeting.duration_ms or 0), "player-time", valign=Gtk.Align.CENTER)
        bar.append(self.total)

        rates = Gio.Menu()
        for rate in ("0.75", "1", "1.25", "1.5", "1.75", "2"):
            item = Gio.MenuItem.new(text("format.playbackRate", rate=Decimal(rate)), None)
            item.set_action_and_target_value("win.rate", GLib.Variant("s", rate))
            rates.append_item(item)
        # The internal toggle owns GTK's AT-SPI action. Name that real
        # control and expose the surrounding MenuButton as a group.
        self.rate_button = Gtk.MenuButton(
            menu_model=rates,
            always_show_arrow=False,
            css_classes=["flat", "player-rate-badge"],
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.GROUP,
        )
        self.rate_button.get_popover().connect("map", self._name_rate_items)
        self.rate_changed(self.window._rate)
        bar.append(self.rate_button)

        transport = Adw.BreakpointBin(
            child=bar,
            width_request=260,
            height_request=70,
            valign=Gtk.Align.END,
            hexpand=True,
        )
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 480sp"))
        compact.add_setter(self.elapsed, "visible", False)
        compact.add_setter(self.total, "visible", False)
        compact.add_setter(bar, "spacing", 6)
        compact.add_setter(bar, "margin-start", 12)
        compact.add_setter(bar, "margin-end", 12)
        compact.add_setter(bar, "margin-bottom", 10)
        transport.add_breakpoint(compact)
        self.player_overlay = transport
        overlay.add_overlay(transport)
        overlay.set_measure_overlay(transport, False)

    def _name_rate_items(self, widget, *_args):
        # GTK's model buttons do not consistently derive an AT-SPI name from
        # their text. Use the displayed label from our fixed menu model.
        if widget.find_property("text"):
            caption = widget.get_property("text")
            if caption in {text("format.playbackRate", rate=Decimal(rate)) for rate in self.window.RATES}:
                if not getattr(widget, "_cassini_rate_name", False):
                    widget._cassini_rate_name = True
                    widget.connect("notify::active", self._name_rate_items)
                widget.reset_relation(Gtk.AccessibleRelation.LABELLED_BY)
                widget.update_property([Gtk.AccessibleProperty.LABEL], [caption])
        child = widget.get_first_child()
        while child:
            self._name_rate_items(child)
            child = child.get_next_sibling()

    def rate_changed(self, rate):
        value = text("format.playbackRate", rate=Decimal(f"{rate:g}"))
        self.rate_button.set_label(value)
        name = text("player.speed.a11y", rate=value)
        self.rate_button.update_property([Gtk.AccessibleProperty.LABEL], [name])
        self.rate_button.get_first_child().update_property([Gtk.AccessibleProperty.LABEL], [name])

    def seek_to(self, position):
        if not self._can_listen or not self.window._ensure_player(self.meeting):
            return False
        self.player.seek(int(position))
        return True

    def _seek(self, _scale, _scroll, value):
        if not self._updating:
            self.seek_to(value)
        return True

    def _colour(self, speaker):
        return speaker_colour(speaker, dark=Adw.StyleManager.get_default().get_dark())

    def _turn_markup(self, turn, *, position=None):
        highlights = getattr(self, "_find_row_highlights", None)
        row_hl = highlights.get(turn.index) if highlights else None
        return reading_markup(
            turn,
            self.player.position if position is None else position,
            lambda speaker: self.meeting.speaker_for(speaker).name,
            dark=Adw.StyleManager.get_default().get_dark(),
            chip_links=self._exact,
            row_highlights=row_hl,
        )

    def _render_words(self, turn, words):
        colour = words.get_color() if words.get_root() is not None else None
        rgba = (colour.red, colour.green, colour.blue, colour.alpha) if colour is not None else None
        highlights = getattr(self, "_find_row_highlights", None)
        row_hl = highlights.get(turn.index) if highlights else None
        signature = (
            reading_markup_signature(
                turn,
                self.player.position,
                lambda speaker: self.meeting.speaker_for(speaker).name,
                dark=Adw.StyleManager.get_default().get_dark(),
                row_highlights=row_hl,
            ),
            rgba,
        )
        if getattr(words, "_reading_signature", None) != signature:
            markup = self._turn_markup(turn)
            if rgba is not None:
                markup = bridge_link_spaces(markup, rgba)
            words.set_markup(markup)
            words._reading_signature = signature

    def _bind(self, _factory, item):
        turn = item.get_item().turn
        item.set_child(self._build_row(turn))

    def _build_row(self, turn, *, register=True):
        row = ExactTurnRow() if self._exact else Gtk.Box(spacing=16, margin_bottom=24)
        row._turn = turn
        names = box(5, width_request=120, valign=Gtk.Align.START)
        caption = label("", "heading", xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=16)
        name = Gtk.Button(
            child=caption,
            css_classes=["flat", "link", "transcript-name"],
            sensitive=self._can_listen,
        )
        name.connect("clicked", lambda button: self.listen_from_row(turn, button))
        names.append(name)
        row._over_label = label("", "small", "dim-label", xalign=0, wrap=True)
        names.append(row._over_label)
        row._auto_tag = None
        time = Gtk.Button(
            label=fmt.position(turn.start_ms),
            css_classes=["flat", "link", "small", "transcript-time"],
            halign=Gtk.Align.START,
            tooltip_text=text("player.playFromHere"),
            sensitive=self._can_listen,
        )
        time.update_property(
            [Gtk.AccessibleProperty.LABEL], [text("player.playFromHere") + ", " + fmt.position(turn.start_ms)]
        )
        time.connect("clicked", lambda *_: self.play_from(turn.start_ms))
        names.append(time)
        label_type = TranscriptLabel if self._exact else Gtk.Label
        words = label_type(
            xalign=0,
            yalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            hexpand=True,
            use_markup=True,
            css_classes=["words"],
        )
        words.connect("activate-link", lambda _label, uri: self._word_clicked(turn, uri))
        row.append(names)
        row.append(words)
        if not self._exact:
            self._render_words(turn, words)
        if register:
            words.connect("map", lambda words: self._render_words(turn, words))
        if register:
            self.bound[turn.index] = (turn, words, row)
        if register:
            self._refresh_header(turn, row)
        if turn.index in self._highlighted_rows:
            words.add_css_class("listen-current")
        return row

    def _metric_transcript_markup(self, turn):
        return self._turn_markup(turn, position=-1)

    def _set_probe_turn(self, turn, probe, markup):
        probe.get_last_child().set_markup(markup)
        self._refresh_header(turn, probe)
        probe.get_first_child().get_last_child().set_label(fmt.position(turn.start_ms))

    def _refresh_header(self, turn, row):
        # Idle callbacks run outside a caller's temporary locale context.
        with using_language(self._header_signature[-1] if self._header_signature else language()):
            self._render_header(turn, row)

    def _render_header(self, turn, row):
        if self._exact:
            row.set_margin_bottom(0 if getattr(turn, "has_more", False) else 24)
        speaker = self.meeting.speaker_for(turn.speaker)
        recognised = speaker.provenance == "recognised" and self.window.get_application().voice_store.has_match(
            self.meeting.directory, speaker.id
        )
        headers = row.get_first_child()
        name, time = headers.get_first_child(), headers.get_last_child()
        interactive = isinstance(name, Gtk.Button)
        name_tip = text("listen.speakerName.tooltip", name=speaker.name) if interactive else speaker.name
        time_tip = text("player.playFromHere") if isinstance(time, Gtk.Button) else None
        tag_name, tag_tip = text("common.autoTag"), text("common.autoTag.tooltip")
        caption_text = row_caption(turn, lambda identifier: self.meeting.speaker_for(identifier).name)
        over_tip = text("meeting.turn.over.tooltip") if turn.over else None
        state = (
            speaker.name,
            self._colour(speaker),
            recognised,
            name_tip,
            time_tip,
            tag_name,
            tag_tip,
            turn.continuation,
            caption_text,
            over_tip,
        )
        if getattr(row, "_header_state", None) == state:
            return
        row._header_state = state
        if self._exact:
            row.update_property([Gtk.AccessibleProperty.LABEL], [speaker.name + ", " + fmt.position(turn.start_ms)])
        caption = name.get_child() if interactive else name
        caption.set_markup(f'<span foreground="{state[1]}">{GLib.markup_escape_text(speaker.name)}</span>')
        name.set_visible(not turn.continuation)
        row._over_label.set_label(caption_text)
        row._over_label.set_visible(bool(caption_text))
        row._over_label.set_tooltip_text(over_tip)
        if over_tip:
            row._over_label.update_property([Gtk.AccessibleProperty.DESCRIPTION], [over_tip])
        else:
            row._over_label.reset_property(Gtk.AccessibleProperty.DESCRIPTION)
        name.set_tooltip_text(name_tip)
        if interactive:
            name.update_property([Gtk.AccessibleProperty.LABEL], [name_tip])
        tag = row._auto_tag
        if recognised and not turn.continuation:
            if tag is None:
                tag = label(tag_name, "auto-tag", halign=Gtk.Align.START)
                headers.insert_child_after(tag, name)
                row._auto_tag = tag
            tag.set_label(tag_name)
            tag.set_tooltip_text(tag_tip)
            tag.update_property([Gtk.AccessibleProperty.DESCRIPTION], [tag_tip])
        elif tag is not None:
            headers.remove(tag)
            row._auto_tag = None
        time.set_tooltip_text(time_tip)
        if isinstance(time, Gtk.Button):
            time.update_property([Gtk.AccessibleProperty.LABEL], [time_tip + ", " + fmt.position(turn.start_ms)])

    def _queue_transcript_headers(self, signature):
        if signature == self._header_signature:
            return
        if getattr(self, "_exact", False) and changed_header_rows(self._turns, self._header_signature, signature):
            self.list.invalidate()
        targets = changed_header_rows(
            (turn for turn, _words, _row in self.bound.values()), self._header_signature, signature
        )
        self._header_signature = signature
        # Keep still-bound pending updates when a second rename arrives before
        # the idle batch runs; each widget reads the newest meeting metadata.
        pending = {
            turn.index: (turn, words, row)
            for turn, words, row in self._header_pending
            if self.bound.get(turn.index, (None, None, None))[2] is row
        }
        pending.update((turn.index, self.bound[turn.index]) for turn in targets)
        self._header_pending = list(pending.values())
        if self._header_pending and not self._header_source:
            self._header_source = GLib.idle_add(self._refresh_headers)

    def _refresh_headers(self):
        deadline = GLib.get_monotonic_time() + 4000
        for _ in range(16):
            if not self._header_pending:
                self._header_source = 0
                return GLib.SOURCE_REMOVE
            turn, words, row = self._header_pending.pop()
            current = self.bound.get(turn.index)
            if current is not None and current[2] is row:
                self._refresh_header(turn, row)
                if turn.asides:
                    self._render_words(turn, words)
            if GLib.get_monotonic_time() >= deadline:
                break
        return GLib.SOURCE_CONTINUE

    def _play_focused_row(self):
        focus = self.window.get_focus()
        for turn, _words, row in self.bound.values():
            # Keyboard navigation focuses GtkListItemWidget. Descendant
            # name/time controls share that same turn, without a second Box stop.
            if focus is row.get_parent() or focus is row or (focus is not None and focus.is_ancestor(row)):
                return self.play_from(turn.start_ms)
        return False

    def play_from(self, position):
        if not self._can_listen or not self.window._ensure_player(self.meeting):
            return False
        self.player.seek(position)
        self.player.play()
        return True

    def listen_from_row(self, row_turn, origin):
        index = self._row_listening.get(row_turn.index)
        if index is not None and self._can_listen and self.window._ensure_player(self.meeting):
            self._listen_origin = origin
            self.window.listen.start(row_turn.speaker, self._local_turn_index[index])

    def _hand_scroll(self, *_):
        if self._exact:
            self.list.cancel_pending_scroll()
        self._follow_until = GLib.get_monotonic_time() + 4_000_000
        return False

    def _index_listening_rows(self, meeting, layout=None):
        # Rejoined words and inline replies both belong to the visible host row.
        if layout is None:
            layout = meeting.turns
        rows = {}
        for row in layout:
            for owner in [row] + [aside for _, aside in row.asides]:
                for index in owner.word_indices:
                    rows[index] = row.index
        previous = getattr(self, "_turn_rows", {})
        self._turn_rows, self._row_listening, self._local_turn_index = {}, {}, {}
        counts = {}
        for turn in meeting.listening_turns:
            positions = frozenset(rows[i] for i in turn.word_indices if i in rows)
            self._turn_rows[turn.index] = positions
            for row_index in positions:
                if layout[row_index].speaker == turn.speaker:
                    self._row_listening.setdefault(row_index, turn.index)
            self._local_turn_index[turn.index] = counts.get(turn.speaker, 0)
            counts[turn.speaker] = self._local_turn_index[turn.index] + 1
        if previous != self._turn_rows:
            self._highlight_target = None

    def _listening_requested(self, *_):
        dialog = self._dialog_focus
        if dialog is not None and dialog.closed and not getattr(dialog, "_recompute", None):
            # A new explicit request supersedes the closed dialog's focus return.
            self._clear_dialog_focus()
        self._follow_until = 0
        self._scroll_listening_turn()

    def _listening_revealed(self, *_):
        if self.listening_bar.get_child_revealed() and GLib.get_monotonic_time() >= self._follow_until:
            self._scroll_listening_turn()

    def _scroll_listening_turn(self):
        if self._updating_transcript:
            return
        turn = self.window.listen.turn
        positions = self._turn_rows.get(turn.index, ()) if turn else ()
        if positions:
            self._reading_index = min(positions)
            self.list.scroll_to(self._reading_index, Gtk.ListScrollFlags.NONE, None)

    def _highlight_listening(self):
        if self._updating_transcript:
            return
        turn = self.window.listen.turn
        target = (turn.speaker, turn.index) if turn else None
        positions = self._turn_rows.get(turn.index, frozenset()) if turn else frozenset()
        for index in positions ^ self._highlighted_rows:
            if index in self.bound:
                words = self.bound[index][1]
                (words.add_css_class if index in positions else words.remove_css_class)("listen-current")
        self._highlighted_rows = positions
        if target != self._highlight_target and positions and GLib.get_monotonic_time() >= self._follow_until:
            self._scroll_listening_turn()
        self._highlight_target = target

    def _unbind(self, _factory, item):
        turn = item.get_item().turn if item.get_item() else None
        if turn is not None:
            current = self.bound.get(turn.index)
            # A splice can bind the replacement before unbinding the old row
            # at the same index. Only remove the row this item still owns.
            if current is not None and current[2] is item.get_child():
                self.bound.pop(turn.index)
        item.set_child(None)

    def _word_clicked(self, turn, uri):
        try:
            word = reading_word(turn, uri)
        except (ValueError, IndexError):
            return True
        if not self.window._ensure_player(self.meeting):
            return True
        GLib.idle_add(lambda: (self.player.seek(word.start_ms), False)[1])
        return True

    def refresh_transcription_progress(self, *, stopping=None):
        meeting = self.meeting
        is_transcribing = meeting.transcript in ("queued", "running")
        app = self.window.get_application()
        if stopping is None:
            stopping = app.transcription_stopping(meeting) if hasattr(app, "transcription_stopping") else False
        can_stop = bool(meeting.lifecycle == "saved" and meeting.metadata_editable)
        if is_transcribing:
            live_progress = app.transcription_progress(meeting) if hasattr(app, "transcription_progress") else None
            self.transcribing_row.update_state(
                meeting,
                live_progress=live_progress,
                can_stop=can_stop,
                stopping=stopping,
                lang=language(),
            )
            self.transcribing_row.set_visible(True)
            self.stop_transcription_button = self.transcribing_row.stop_button if can_stop else None
        else:
            self.transcribing_row.popdown()
            self.transcribing_row.set_visible(False)
            self.stop_transcription_button = None

    def update(self, meeting):
        self._updating_transcript = True
        self._updating_rows = True
        self.meeting = meeting
        self._sync_back_button()
        is_transcribing = meeting.transcript in ("queued", "running")
        if is_transcribing and self._header_scrolled:
            self._set_header_scrolled(False)
        layout = getattr(meeting.turns, "layout", meeting.turns) if self._exact else meeting.turns
        subtitle = fmt.preview_subtitle(
            meeting.display_start,
            meeting.duration_ms,
            now_ms=self.window.now_ms,
            tz=self.window.tz,
            received=meeting.origin == "received",
        )
        self.headline.set_title(meeting.title)
        self.headline.set_subtitle(subtitle)
        self.title_label.set_label(meeting.title)
        self.title_label.set_tooltip_text(meeting.title)
        self.subtitle_label.set_label(subtitle)
        self.compact_title.set_label(meeting.title)
        self.compact_subtitle.set_label(subtitle)
        if self.window._external_meeting:
            self.window.set_title(meeting.title)
        self.rename_button.set_visible(bool(self.window.rename_allowed(meeting)))

        self.total.set_label(fmt.position(meeting.duration_ms or 0))
        app = self.window.get_application()
        writing = getattr(app, "writing_available", True)
        stopping = app.transcription_stopping(meeting) if hasattr(app, "transcription_stopping") else False
        self.write_button.set_visible(
            writing and meeting.can_write_down and meeting.transcript == "none" and not stopping
        )
        self.seek.set_range(0, max(1, meeting.duration_ms or 1))
        if self.window._loaded_key == meeting.key:
            can_play = bool(meeting.has_audio and self.player.loaded and not self.window._playback_failed)
        else:
            can_play = bool(self.window.is_playable(meeting))
        for button in self.transport_buttons:
            button.set_sensitive(can_play)
        self.seek.set_sensitive(can_play)
        self.player_overlay.set_visible(bool(meeting.has_audio))
        self._can_listen = can_play and not is_transcribing
        self.find_row.set_visible(not is_transcribing and (bool(meeting.words) or bool(self._find_query)))
        self.transcribing_blank.set_visible(is_transcribing)
        self.scroller.set_opacity(0.0 if is_transcribing else 1.0)
        self.scroller.set_can_target(not is_transcribing)
        self.scroller.set_can_focus(not is_transcribing)
        self.refresh_transcription_progress(stopping=stopping)

        if self._turns != layout or self._listening_turns is not meeting.listening_turns:
            self._index_listening_rows(meeting, layout)
        if (not can_play or is_transcribing) and self.window.listen.active:
            self.window.listen.stop()
        if self._listening_turns is not meeting.listening_turns:
            self._listening_turns = meeting.listening_turns
            self.timeline.set_turns(meeting.listening_turns)
            self.window.listen.set_turns(meeting.listening_turns)
        self._listening_changed()
        source = self.window._activity_source
        self.set_connected(source.connected if source else False)
        if self.speakers_dialog and not self.speakers_dialog.closed:
            self.speakers_dialog.update(meeting)
        undo = bool(meeting.metadata_editable and meeting.metadata.get("speakerMerges"))
        self.window.lookup_action("undo-speaker-merge").set_enabled(undo and self._speaker_future is None)
        name_action = self.window.lookup_action("name-speakers")
        if name_action is not None:
            name_action.set_enabled(bool(meeting.speakers and meeting.metadata_editable))
        visible_speakers = meeting.preview_speakers
        dark = self._style_manager.get_dark()
        matches = frozenset(
            s.id
            for s in visible_speakers
            if meeting.metadata_editable
            and self.window.get_application().voice_store.has_match(meeting.directory, s.id)
        )
        self._queue_transcript_headers(
            (dark, matches, meeting.speakers, meeting.metadata.get("speakerMerges"), language())
        )
        trash_allowed = self.window.get_application().meeting_trash.allowed(meeting, self.window)
        trash_failure = self.window.get_application().meeting_trash.errors.get(meeting.directory)
        self.trash_button.set_visible(
            trash_allowed or (meeting.published_file is not None and not meeting.metadata_editable)
        )
        folder_name = (meeting.metadata.get("received") or {}).get("originalFolderName")
        signature = (
            meeting.key,
            undo,
            meeting.metadata.get("speakerMerges"),
            trash_allowed,
            trash_failure,
            matches,
            # Library scans reparse the same words. Identity would rebuild the
            # rows and unrealize a button during GTK's delayed activation,
            # cancelling its pending click (and losing keyboard focus).
            meeting.words,
            meeting.turns,
            meeting.speakers,
            visible_speakers,
            meeting.interrupted_at_ms,
            meeting.transcript,
            stopping,
            meeting.eta_minutes,
            meeting.error,
            meeting.publication_notice,
            meeting.metadata_editable,
            meeting.has_audio,
            folder_name,
            dark,
        )
        if signature == self._signature:
            self._updating_transcript = False
            self._updating_rows = False
            self._listening_changed()
            return
        self._signature = signature
        self.more_menu.remove_all()
        self.more_menu.append_item(self.window.rename_item())
        if meeting.speakers and (meeting.metadata_editable or meeting.published_file is not None):
            self.more_menu.append(text("meeting.nameSpeakers"), "win.name-speakers")
        self.more_menu.append(text("details.menu"), "win.recording-details")
        self.more_menu.append(text("common.exportEllipsis"), "win.export")
        if undo:
            self.more_menu.append(text("meeting.undoMerge"), "win.undo-speaker-merge")
        if trash_allowed or (meeting.published_file is not None and not meeting.metadata_editable):
            section = Gio.Menu()
            from .meeting_trash import menu_item

            section.append_item(menu_item())
            self.more_menu.append_section(None, section)

        while child := self.speakers.get_first_child():
            self.speakers.remove(child)
        self._speaker_rows.clear()
        for speaker in visible_speakers:
            self.speakers.append(self._inline_speaker(speaker, matches))
        show_speakers = bool(visible_speakers) and not is_transcribing
        self.speakers.set_visible(show_speakers)
        self.speakers_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC if len(visible_speakers) > 6 else Gtk.PolicyType.NEVER,
        )
        self.speakers_scroller.set_visible(show_speakers)
        self._updating_rows = False
        self._finish_merge_transition()
        self._listening_changed()

        while child := self.lines.get_first_child():
            self.lines.remove(child)
        if not is_transcribing and meeting.origin == "received" and isinstance(folder_name, str) and folder_name:
            card = Gtk.Box(spacing=12, css_classes=["received-folder"], valign=Gtk.Align.START, margin_bottom=8)
            card.append(
                Gtk.Image(
                    icon_name="folder-symbolic",
                    valign=Gtk.Align.START,
                    accessible_role=Gtk.AccessibleRole.PRESENTATION,
                )
            )
            body = label("", wrap=True, xalign=0, hexpand=True)
            body.set_markup(text("received.folderCard", folder="<b>" + GLib.markup_escape_text(folder_name) + "</b>"))
            card.append(body)
            self.lines.append(card)
        publication = self.window.publication_line(meeting)
        if publication:
            self.lines.append(publication)
        if not is_transcribing and meeting.lifecycle == "saved" and meeting.interrupted_at_ms is not None:
            self.lines.append(
                InlineLine("meeting.interruptedAt", info=True, position=fmt.position(meeting.interrupted_at_ms))
            )
        trash_error = self.window.trash_error_line(meeting)
        if trash_error:
            self.lines.append(trash_error)
        elif not is_transcribing and meeting.transcript == "damaged":
            self.lines.append(InlineLine("errors.transcriptDamaged.line"))
        elif not is_transcribing and meeting.transcript == "failed":
            app = self.window.get_application()
            retry = getattr(app, "write_down", None)
            self.lines.append(
                InlineLine(
                    "errors.transcribeFailed.line",
                    action="common.tryAgain"
                    if meeting.metadata_editable and getattr(app, "writing_available", True)
                    else None,
                    callback=(lambda: retry(meeting)) if retry and meeting.metadata_editable else None,
                )
            )
        elif not is_transcribing and meeting.no_speech:
            app = self.window.get_application()
            self.lines.append(
                InlineLine(
                    "transcript.noSpeech",
                    info=True,
                    action="transcript.writeDownAgain"
                    if meeting.can_write_down and getattr(app, "writing_available", True)
                    else None,
                    callback=lambda: app.write_down(meeting),
                )
            )
        self.lines.set_visible(self.lines.get_first_child() is not None)

        # Only changed timed text replaces transcript items. Metadata/theme
        # updates preserve their focused widgets and refresh headers in batches.
        turns_changed = self._turns is not layout
        if turns_changed:
            common = 0
            for before, after in zip(self._turns, layout, strict=False):
                if before != after:
                    break
                common += 1
            removed = len(self._turns) - common
            additions = [] if self._exact else [TurnItem(turn) for turn in layout[common:]]
            if self._exact:
                self.list.set_turns(layout, common)
            elif removed or additions:
                self.store.splice(common, removed, additions)
            self._turns = layout
            self._follow_rows = getattr(layout, "follow", None) or FollowRows(layout)
            if self._find_query:
                self._recompute_find_matches(scroll=False)
        self._updating_transcript = False
        self._highlight_listening()
        self._position()
        self._schedule_rows_ready()

    def _schedule_rows_ready(self, *_):
        if not self._closed and self.speakers.get_mapped() and not self._rows_rebuilt_tick:
            self._rows_rebuilt_tick = self.add_tick_callback(self._rows_ready)

    def _finish_merge_transition(self):
        # The listening handoff depends on the new model, never sidebar layout.
        if self._merge_transition and self._merge_transition[0] not in self._speaker_rows:
            _source, following = self._merge_transition
            self._merge_transition = None
            if following in self._speaker_rows:
                self.window.listen.start(following, 0, play=False)
                if self._dialog_focus is None:
                    self.listening_bar.focus_merge(following)

    def _rows_ready(self, _page, _clock):
        # Allocation alone can survive unmapping. Announce only the current
        # installed rows, wired, mapped and allocated for GTK activation.
        if self._closed or not self.speakers.get_mapped():
            self._rows_rebuilt_tick = 0
            return GLib.SOURCE_REMOVE
        if not self.get_mapped() or any(
            not row.get_mapped() or not row.get_realized() or row.get_width() <= 0
            for row, _target, _when in self._speaker_rows.values()
        ):
            return GLib.SOURCE_CONTINUE
        self._rows_rebuilt_tick = 0
        self.emit("rows-rebuilt")
        self._queue_dialog_focus()
        return GLib.SOURCE_REMOVE

    def _inline_speaker(self, speaker, matches):
        turns = self.window.listen.turns_of(speaker.id)
        when = ""
        if turns:
            position = fmt.position(turns[0].start_ms)
            when = (
                text("speakers.when.once", position=position)
                if len(turns) == 1
                else plural("speakers.when.times", len(turns), count=len(turns), position=position)
            )
        caption = {"user": "namedByYou", "recognised": "recognised"}.get(speaker.provenance, "notNamed")
        provenance = (
            (text("common.yourMicrophone") if speaker.id == "me" else text("recordFirst.sound.title"))
            if speaker.provenance == "source"
            else text("meeting.speakerCaption." + caption)
        )
        if speaker.provenance == "recognised" and speaker.id not in matches:
            provenance = ""
        description = (
            text("speakers.row.a11y", name=speaker.name, provenance=provenance, when=when)
            if provenance and when
            else ", ".join(part for part in (speaker.name, provenance, when) if part)
        )
        slot = speaker_slot(speaker)
        content = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        dot = Gtk.Box(
            valign=Gtk.Align.CENTER,
            css_classes=["speaker-dot", f"speaker-dot-{slot}"],
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        content.append(dot)
        name_classes = ["inline-speaker-name"]
        if slot == 0:
            name_classes.append("speaker-unnamed")
        name_label = label(
            speaker.name,
            *name_classes,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=24,
        )
        content.append(name_label)
        if speaker.provenance == "recognised" and speaker.id in matches:
            tag = label(text("common.autoTag"), "auto-tag", valign=Gtk.Align.CENTER)
            tag.set_tooltip_text(text("common.autoTag.tooltip"))
            tag.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text("common.autoTag.tooltip")])
            content.append(tag)
        if self.meeting.metadata_editable:
            button = Gtk.Button(
                child=content,
                css_classes=["flat", "inline-speaker-button"],
                tooltip_text=text("meeting.nameSpeakers"),
                valign=Gtk.Align.CENTER,
            )
            button.update_property(
                [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                [description, text("meeting.nameSpeakers")],
            )
            button.connect("clicked", lambda *_: self.name_speakers(speaker.id))
            focus = Gtk.EventControllerFocus()
            focus.connect("enter", lambda *_, b=button: self._scroll_speaker(b))
            button.add_controller(focus)
            self._speaker_rows[speaker.id] = button, button, when
            return button
        button = Gtk.Button(
            child=content,
            css_classes=["flat", "inline-speaker-button"],
            tooltip_text=text("listen.speakerName.tooltip", name=speaker.name),
            sensitive=bool(turns and self._can_listen),
            valign=Gtk.Align.CENTER,
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [description])
        button.connect("clicked", lambda *_: self._listen_speaker(speaker.id))
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_, b=button: self._scroll_speaker(b))
        button.add_controller(focus)
        self._speaker_rows[speaker.id] = button, button, when
        return button

    def stop_listening(self):
        speaker = self.window.listen.speaker
        self.window.listen.stop()
        origin = self._listen_origin
        if (origin is None or not origin.get_mapped()) and speaker in self._speaker_rows:
            origin = self._speaker_rows[speaker][1]
        if origin is not None and origin.get_mapped() and origin.get_focusable():
            origin.grab_focus()

    def _listen_speaker(self, identifier):
        if not self.window._ensure_player(self.meeting):
            return
        if identifier in self._speaker_rows:
            self._listen_origin = self._speaker_rows[identifier][1]
        listen = self.window.listen
        if listen.active and listen.speaker == identifier:
            self.player.pause()
            listen.stop()
        else:
            listen.start(identifier)

    def _listening_changed(self, *_):
        for _turn, _words, row in self.bound.values():
            headers = row.get_first_child()
            headers.get_first_child().set_sensitive(self._can_listen)
            headers.get_last_child().set_sensitive(self._can_listen)
        listen = self.window.listen
        for identifier, (row, target, _when) in self._speaker_rows.items():
            if not self.meeting.metadata_editable:
                target.set_sensitive(bool(listen.turns_of(identifier) and self._can_listen))
            active = listen.active and listen.speaker == identifier
            (row.add_css_class if active else row.remove_css_class)("listening")
            if active and self._listening_target != (identifier, row):
                self._listening_target = identifier, row
                self._scroll_speaker(row)
        if not listen.active:
            self._listening_target = None
            # Stop invalidates a delayed Name return. A merge must still await
            # its destination row while the removed source stops listening.
            if not getattr(self._dialog_focus, "_recompute", None):
                self._clear_dialog_focus()
        self._highlight_listening()
        self.listening_bar.update()
        self.timeline.set_target(listen.turns_of(listen.speaker) if listen.active else ())
        self.listening_keys.refresh()

    def _scroll_speaker(self, row):
        self._scroll_target = row
        row.add_tick_callback(self._scroll_listening)

    def _scroll_listening(self, row, _clock):
        if self._closed or self._scroll_target is not row:
            return GLib.SOURCE_REMOVE
        if not row.get_height():
            return GLib.SOURCE_CONTINUE
        ok, bounds = row.compute_bounds(self.speakers_scroller)
        if ok:
            adjustment = self.speakers_scroller.get_vadjustment()
            top = adjustment.get_value() + bounds.get_y()
            adjustment.clamp_page(top, top + bounds.get_height())
        return GLib.SOURCE_REMOVE

    def name_speakers(self, identifier=None, *, from_listening=False):
        if not self.meeting.metadata_editable or not self.meeting.speakers:
            return
        from .speakers_dialog import SpeakersDialog

        if self.meeting.has_audio:
            self.window._ensure_player(self.meeting)
        self._clear_dialog_focus()
        if self.speakers_dialog and not self.speakers_dialog.closed:
            self.speakers_dialog.close()
        visible = {s.id for s in self.meeting.speakers}
        selected = identifier or self._selected_speaker
        self._selected_speaker = selected if selected in visible else self.meeting.speakers[0].id
        self.speakers_dialog = SpeakersDialog(self, self._selected_speaker)
        self.speakers_dialog.present(self.window)
        if from_listening:
            self.speakers_dialog.play.grab_focus()
            self._dialog_focus = self.speakers_dialog
            for obj, signal in (
                (self.speakers_dialog, "closed"),
                (self.speakers_dialog, "unmap"),
                (self.window, "notify::visible-dialog"),
                (self.listening_bar, "notify::child-revealed"),
                (self, "map"),
            ):
                self._dialog_focus_signals.append((obj, obj.connect(signal, self._queue_dialog_focus)))
        else:
            self.speakers_dialog.entry.grab_focus()

    def _clear_dialog_focus(self):
        if self._dialog_focus_idle:
            GLib.source_remove(self._dialog_focus_idle)
            self._dialog_focus_idle = 0
        for obj, signal in self._dialog_focus_signals:
            obj.disconnect(signal)
        self._dialog_focus_signals.clear()
        self._dialog_focus = None

    def _queue_dialog_focus(self, *_):
        if not self._closed and self._dialog_focus is not None and not self._dialog_focus_idle:
            self._dialog_focus_idle = GLib.idle_add(self._restore_dialog_focus)

    def _restore_dialog_focus(self):
        self._dialog_focus_idle = 0
        dialog = self._dialog_focus
        if dialog is None or not dialog.closed or dialog.get_mapped():
            return GLib.SOURCE_REMOVE
        visible = self.window.get_visible_dialog()
        if visible is not None:
            if visible is not dialog:
                self._clear_dialog_focus()
            return GLib.SOURCE_REMOVE
        merged = getattr(dialog, "_recompute", None)
        if merged:
            source, destination = merged
            if hasattr(self.window, "split") and self.window.split and self.window.split.get_collapsed():
                self.window.split.set_show_content(True)
            if self._header_scrolled:
                self._set_header_scrolled(False)
            if source in self._speaker_rows or self._rows_rebuilt_tick or self._merge_transition:
                return GLib.SOURCE_REMOVE
            if destination not in self._speaker_rows:
                return GLib.SOURCE_REMOVE
            row, target, _when = self._speaker_rows[destination]
            if not isinstance(target, Gtk.Button) and self.play.get_mapped():
                target = self.play
            self._scroll_speaker(row)
        else:
            if not self.listening_bar.get_child_revealed():
                return GLib.SOURCE_REMOVE
            target = self.listening_bar.name_focus_target()
        if target.get_mapped() and target.is_sensitive() and target.grab_focus():
            self._clear_dialog_focus()
        else:
            # Mapping and sensitivity can follow the dialog's modal teardown.
            # Subscribe once to this control, rather than retrying on a timer.
            if not any(obj is target for obj, _signal in self._dialog_focus_signals):
                for signal in ("map", "state-flags-changed"):
                    self._dialog_focus_signals.append((target, target.connect(signal, self._queue_dialog_focus)))
        return GLib.SOURCE_REMOVE

    def prepare_speaker_merge(self, source, target):
        if not self.window.listen.active or self.window.listen.speaker != source:
            return None
        order = list(self.meeting.speakers)
        index = next((i for i, s in enumerate(order) if s.id == source), len(order))
        following = next(
            (
                s.id
                for s in order[index + 1 :]
                if s.id != target and s.provenance == "unnamed" and self.window.listen.turns_of(s.id)
            ),
            None,
        )
        self.player.pause()
        return source, following

    def speaker_merged(self, source, target, transition=None):
        # A filesystem notification may have delivered the merged model before
        # the worker's success callback. Keep the pre-edit pane order.
        self._merge_transition = transition
        self._finish_merge_transition()
        name = next((s.name for s in self.meeting.speakers if s.id == target), target)
        if getattr(self, "_merge_toast", None) is not None:
            self._merge_toast.dismiss()
        toast = Adw.Toast(
            title=GLib.markup_escape_text(text("speakers.mergedToast", name=name)),
            button_label=text("common.undo"),
            action_name="win.undo-speaker-merge",
        )
        self._merge_toast = toast
        self.window.toasts.add_toast(toast)

    def merge_speaker(self, source, target):
        if not self.meeting.metadata_editable or self._speaker_future is not None or self.window.monitor._closed:
            return
        from .speaker_edits import merge_into

        transition = self.prepare_speaker_merge(source, target)
        directory = self.meeting.directory
        meeting_key = self.meeting.key
        app = self.window.get_application()
        self._speaker_future = self.window.monitor._executor.submit(
            merge_into, directory, source, target, scanner=self.window.monitor.scanner
        )
        self.listening_bar.update()
        self._speaker_future.add_done_callback(
            lambda f: GLib.idle_add(self._speaker_merged, f, directory, meeting_key, app, source, target, transition)
        )

    def _speaker_merged(self, future, directory, meeting_key, app, source, target, transition):
        from concurrent.futures import CancelledError

        if self._speaker_future is future:
            self._speaker_future = None
        same_meeting = not self._closed and self.meeting.key == meeting_key and self.meeting.directory == directory
        try:
            future.result()
        except CancelledError:
            return False
        except (OSError, ValueError):
            if same_meeting:
                self.window.toast(text("speakers.changeFailed"))
        else:
            app.recompute_voices(directory, (source, target))
            if same_meeting:
                self.speaker_merged(source, target, transition)
            if not self.window.monitor._closed:
                self.window.monitor.refresh_now()
        if same_meeting:
            self.listening_bar.update()
        return False

    def undo_speaker_merge(self):
        if self._closed or self._speaker_future is not None or not self.meeting.metadata_editable:
            return
        from .library_edits import transaction
        from .meeting_store import read_metadata
        from .speaker_edits import undo_merge

        directory = self.meeting.directory
        meeting_key = self.meeting.key
        app = self.window.get_application()
        self.window.lookup_action("undo-speaker-merge").set_enabled(False)

        def undo():
            with transaction(directory):
                history = read_metadata(directory).get("speakerMerges", [])
                undo_merge(directory, scanner=self.window.monitor.scanner)
                affected = (history[-1]["from"], history[-1]["into"]) if history else ()
                return directory, affected

        self._speaker_future = self.window.monitor._executor.submit(undo)
        self._speaker_future.add_done_callback(
            lambda f: GLib.idle_add(self._merge_undone, f, directory, meeting_key, app)
        )

    def _merge_undone(self, future, orig_directory=None, meeting_key=None, app=None):
        from concurrent.futures import CancelledError

        if orig_directory is None:
            orig_directory = self.meeting.directory
        if meeting_key is None:
            meeting_key = getattr(self.meeting, "key", None)
        if app is None:
            app = self.window.get_application()
        if self._speaker_future is future:
            self._speaker_future = None
        same_meeting = (
            not self._closed
            and getattr(self.meeting, "key", None) == meeting_key
            and self.meeting.directory == orig_directory
        )
        try:
            directory, affected = future.result()
        except CancelledError:
            pass
        except (OSError, ValueError):
            if same_meeting:
                if getattr(self, "_merge_toast", None) is not None:
                    self._merge_toast.dismiss()
                toast = Adw.Toast(
                    title=GLib.markup_escape_text(text("speakers.changeFailed")),
                    button_label=text("common.tryAgain"),
                    action_name="win.undo-speaker-merge",
                )
                self._merge_toast = toast
                self.window.toasts.add_toast(toast)
                self.window.lookup_action("undo-speaker-merge").set_enabled(True)
        else:
            if affected:
                app.recompute_voices(directory, affected)
            if not self.window.monitor._closed:
                self.window.monitor.refresh_now()
        return False

    def _position(self, _player=None, property=None):
        self.listening_bar.update_navigation()
        self.timeline.position_changed()
        loaded_here = self.window._loaded_key == self.meeting.key
        position_ms = self.player.position if loaded_here else 0
        playing_here = bool(loaded_here and self.player.playing)
        index = self._follow_rows.at(position_ms) if loaded_here else -1
        listen = self.window.listen
        turn = listen.turn
        positions = self._turn_rows.get(turn.index, ()) if turn else ()
        if loaded_here and positions and listen._current(position_ms) is not None:
            index = max(min(positions), min(index, max(positions)))
        explicit_seek = loaded_here and property is not None and property.name == "position" and not playing_here
        resumed = playing_here and not getattr(self, "_was_playing", False)
        self._was_playing = playing_here
        if (
            not self._updating_transcript
            and index >= 0
            and GLib.get_monotonic_time() >= self._follow_until
            and (playing_here or explicit_seek)
            and (index != self._reading_index or explicit_seek or resumed)
        ):
            self._reading_index = index
            self.list.scroll_to(index, Gtk.ListScrollFlags.NONE, None)
        self._updating = True
        self.elapsed.set_label(fmt.position(position_ms))
        self.seek.set_value(position_ms)
        self._updating = False
        self.play.set_icon_name("media-playback-pause-symbolic" if playing_here else "media-playback-start-symbolic")
        play_name = text("player.pause" if playing_here else "common.play")
        self.play.set_tooltip_text(play_name)
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [play_name])
        if self._exact:
            rows = (self.bound[i] for i in self.list.visible_rows if i in self.bound)
        else:
            rows = list(self.bound.values())
        for turn, words, _row in rows:
            self._render_words(turn, words)

    def set_recording_notice(self, line, minutes):
        signature = line, minutes
        if self._notice_signature == signature:
            return
        self._notice_signature = signature
        while child := self.recording_notice.get_first_child():
            self.recording_notice.remove(child)
        self.recording_notice.set_visible(bool(line))
        if line:
            self.recording_notice.append(InlineLine(line, n=minutes))

    def _error(self, *_):
        for button in self.transport_buttons:
            button.set_sensitive(False)
        self.seek.set_sensitive(False)
        self._can_listen = False
        self.window.listen.stop()
        self._listening_changed()

    def close(self):
        self._closed = True
        self.transcribing_row.popdown()
        if hasattr(self, "listening_keys"):
            self.listening_keys.close()
        if self._header_source:
            GLib.source_remove(self._header_source)
            self._header_source = 0
        if getattr(self, "_scroll_header_source", 0):
            GLib.source_remove(self._scroll_header_source)
            self._scroll_header_source = 0
        self._pending_header_scrolled = None
        self._header_pending.clear()
        self._clear_dialog_focus()
        self.timeline.close()
        if self._rows_rebuilt_tick:
            self.remove_tick_callback(self._rows_rebuilt_tick)
            self._rows_rebuilt_tick = 0
        if self.speakers_dialog and not self.speakers_dialog.closed:
            self.speakers_dialog.close()
        if self._speaker_future is not None:
            self._speaker_future.cancel()
        if getattr(self, "_merge_toast", None) is not None:
            self._merge_toast.dismiss()
            self._merge_toast = None
        self._clear_style_refresh()
        if self._vadj_handler:
            self._vadj.disconnect(self._vadj_handler)
            self._vadj_handler = 0
        if self._split_collapsed_handler and hasattr(self.window, "split") and self.window.split is not None:
            self.window.split.disconnect(self._split_collapsed_handler)
            self._split_collapsed_handler = None
        for controller, handler in self._touch_scroll_signals:
            controller.disconnect(handler)
        self._touch_scroll_signals.clear()
        if self._exact:
            self.list.close()
        for handler in self._signals:
            self.player.disconnect(handler)
        self._signals.clear()
        for handler in self._listen_signals:
            self.window.listen.disconnect(handler)
        self._listen_signals.clear()
