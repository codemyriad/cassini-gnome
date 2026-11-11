# SPDX-License-Identifier: GPL-3.0-or-later
"""Pushed Meeting page, with virtualised timed transcript items and transport."""

import os
from decimal import Decimal

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import ux_formats as fmt
from .exact_transcript import ExactTranscript
from .i18n import language, using_language
from .inline_line import InlineLine
from .listening_bar import ListeningBar
from .listening_shortcuts import ListeningShortcuts
from .listening_timeline import ListeningTimeline
from .markup_runs import bridge_link_spaces
from .meeting_text import (
    COLOURS,
    DARK_COLOURS,
    FollowRows,
    changed_header_rows,
    reading_markup,
    reading_markup_signature,
    reading_word,
    row_caption,
    speaking_shares,
)
from .strings import plural, text
from .transcript_label import TranscriptLabel
from .transcript_row import ExactTurnRow


def box(spacing=0, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def label(value, *classes, **kwargs):
    return Gtk.Label(label=value, css_classes=list(classes), **kwargs)


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
        self._scroll_target = None
        self._listening_target = None
        self._highlighted_rows = frozenset()
        self._turn_rows = {}
        self._row_listening = {}
        self._local_turn_index = {}
        self._follow_until = 0
        self._highlight_target = None
        self.settings = Gio.Settings.new(window.get_application().settings_schema)
        outer = box()
        self.set_child(outer)
        self.split = split = Adw.NavigationSplitView(
            min_sidebar_width=260, max_sidebar_width=260, sidebar_width_fraction=0.23, vexpand=True
        )
        responsive = Adw.BreakpointBin(child=split, width_request=360, height_request=100)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        narrow.add_setter(split, "collapsed", True)
        narrow.add_setter(split, "show-content", True)
        responsive.add_breakpoint(narrow)
        outer.append(responsive)
        sidebar = Adw.ToolbarView(css_classes=["library-sidebar"])
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            decoration_layout=":close",
            show_end_title_buttons=False,
            css_classes=["library-header"],
        )
        header.set_title_widget(Gtk.Box())
        back = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text=text("common.backToLibrary"),
            css_classes=["flat"],
            action_name="win.back-library",
        )
        back.update_property([Gtk.AccessibleProperty.LABEL], [text("common.backToLibrary")])
        header.pack_start(back)
        self.capture = window.capture_controls()
        header.pack_end(self.capture)
        sidebar.add_top_bar(header)
        self.speakers = box(20, margin_start=20, margin_end=20, margin_top=18)
        self.speakers.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.speakersPane.a11y")])
        self.speakers_scroller = Gtk.ScrolledWindow(
            child=self.speakers,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        # GTK's automatic focus scrolling would pull an old focused row back
        # over a newly selected listening target. Scroll on focus entry below.
        self.speakers_scroller.get_child().set_scroll_to_focus(False)
        sidebar.set_content(self.speakers_scroller)
        split.set_sidebar(Adw.NavigationPage.new(sidebar, text("common.speakers")))
        content = Adw.ToolbarView()
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            decoration_layout=":close",
            css_classes=["library-header"],
        )
        pane = Gtk.Button(icon_name="system-users-symbolic", tooltip_text=text("common.speakers"), css_classes=["flat"])
        pane.connect("clicked", lambda *_: self.split.set_show_content(False) if self.split.get_collapsed() else None)
        pane.set_visible(False)
        split.connect("notify::collapsed", lambda *_: pane.set_visible(split.get_collapsed()))
        header.pack_start(pane)
        self.headline = Adw.WindowTitle()
        header.set_title_widget(self.headline)
        self.export_button = Gtk.Button(label=text("common.exportEllipsis"), action_name="win.export")
        menu = self.more_menu = Gio.Menu()
        menu.append(text("details.menu"), "win.recording-details")
        menu.append(text("common.exportEllipsis"), "win.export")
        more = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text=text("meeting.moreActions"),
            menu_model=menu,
            css_classes=["flat"],
        )
        more.get_popover().set_halign(Gtk.Align.END)
        more.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.moreActions")])
        header.pack_end(more)
        header.pack_end(self.export_button)
        self.write_button = Gtk.Button(label=text("common.writeDown"), visible=False)
        self.write_button.connect("clicked", lambda *_: window.get_application().write_down(self.meeting))
        header.pack_end(self.write_button)
        content.add_top_bar(header)
        center = box()
        self.recording_notice = box(margin_start=48, margin_end=48, margin_top=8, visible=False)
        self._notice_signature = None
        center.append(self.recording_notice)
        self.lines = box(margin_start=48, margin_end=48, margin_top=8)
        center.append(self.lines)
        self.store = Gio.ListStore.new(TurnItem)
        self._exact = os.environ.get("CASSINI_TRANSCRIPT_RENDER") != "listview"
        if self._exact:
            self.list = ExactTranscript(self)
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
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(self.list)
        center.append(self.scroller)
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
        content.set_content(center)
        split.set_content(Adw.NavigationPage.new(content, meeting.title))
        self._companion = self._companion_card()
        self._companion.set_margin_start(48)
        self._companion.set_margin_end(48)
        center.insert_child_after(self._companion, self.lines)
        self._listen_origin = None
        self.speakers.connect("map", self._schedule_rows_ready)
        self._merge_transition = None
        self._dialog_focus = None
        self._dialog_focus_idle = 0
        self._dialog_focus_signals = []
        self.listening_bar = ListeningBar(self)
        self.listening_bar.connect("notify::child-revealed", self._listening_revealed)
        outer.append(self.listening_bar)
        self._build_player(outer)
        self._style_manager = Adw.StyleManager.get_default()
        self._style_source = 0
        self._style_signals = [
            self._style_manager.connect("notify::" + name, self._style_changed) for name in ("dark", "high-contrast")
        ]
        self._signals = [
            self.player.connect("notify::" + name, self._position) for name in ("position", "playing", "duration")
        ]
        self._signals.append(self.player.connect("error", self._error))
        self._listen_signals = [
            window.listen.connect(signal, self._listening_changed) for signal in ("changed", "stopped")
        ]
        self._listen_signals.append(window.listen.connect("requested", self._listening_requested))
        self.listening_keys = ListeningShortcuts(self)
        self.update(meeting)
        self._position()

    def _style_changed(self, _manager=None, property=None):
        if self._closed:
            return
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
            self.window.get_application().browser_companion_available
            and not connected
            and not self.settings.get_boolean("companion-card-dismissed")
        )
        self._companion.set_visible(visible)

    def _build_player(self, outer):
        bar = Gtk.Box(spacing=14, css_classes=["meeting-player"], margin_start=0)
        self.transport_buttons = []
        for name, icon, action in [
            ("player.back10", "media-seek-backward-symbolic", "win.skip-back"),
            ("common.play", "media-playback-start-symbolic", "win.play-pause"),
            ("player.forward10", "media-seek-forward-symbolic", "win.skip-forward"),
        ]:
            button = Gtk.Button(
                icon_name=icon,
                tooltip_text=text(name),
                action_name=action,
                css_classes=["circular", "flat"],
                valign=Gtk.Align.CENTER,
            )
            button.update_property([Gtk.AccessibleProperty.LABEL], [text(name)])
            if name == "common.play":
                self.play = button
                button.add_css_class("suggested-action")
                button.remove_css_class("flat")
            self.transport_buttons.append(button)
            bar.append(button)
        self.elapsed = label(fmt.position(0), "small", "dim-label")
        bar.append(self.elapsed)
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(1, self.meeting.duration_ms or 1), 100)
        self.seek.set_draw_value(False)
        self.seek.set_hexpand(True)
        self.seek.update_property([Gtk.AccessibleProperty.LABEL], [text("player.a11y")])
        self.seek.connect("change-value", self._seek)
        self.timeline = ListeningTimeline(self, self.seek)
        bar.append(self.timeline)
        self.total = label(fmt.position(self.meeting.duration_ms or 0), "small", "dim-label")
        bar.append(self.total)
        rates = Gio.Menu()
        for rate in ("0.75", "1", "1.25", "1.5", "1.75", "2"):
            item = Gio.MenuItem.new(text("format.playbackRate", rate=Decimal(rate)), None)
            item.set_action_and_target_value("win.rate", GLib.Variant("s", rate))
            rates.append_item(item)
        # The internal toggle owns GTK's AT-SPI action. Name that real
        # control and expose the surrounding MenuButton as a group.
        self.rate_button = Gtk.MenuButton(
            menu_model=rates, css_classes=["flat"], valign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.GROUP
        )
        self.rate_button.get_popover().connect("map", self._name_rate_items)
        self.rate_changed(self.window._rate)
        bar.append(self.rate_button)
        transport = Adw.BreakpointBin(child=bar, width_request=360, height_request=64)
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 480sp"))
        compact.add_setter(self.elapsed, "visible", False)
        compact.add_setter(self.total, "visible", False)
        compact.add_setter(bar, "spacing", 4)
        transport.add_breakpoint(compact)
        outer.append(transport)

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

    def _seek(self, _scale, _scroll, value):
        if not self._updating:
            self.player.seek(int(value))
        return True

    def _colour(self, speaker):
        palette = DARK_COLOURS if Adw.StyleManager.get_default().get_dark() else COLOURS
        return palette[speaker.colour_index % len(palette)]

    def _turn_markup(self, turn, *, position=None):
        return reading_markup(
            turn,
            self.player.position if position is None else position,
            lambda speaker: self.meeting.speaker_for(speaker).name,
            dark=Adw.StyleManager.get_default().get_dark(),
            chip_links=self._exact,
        )

    def _render_words(self, turn, words):
        colour = words.get_color() if words.get_root() is not None else None
        rgba = (colour.red, colour.green, colour.blue, colour.alpha) if colour is not None else None
        signature = (
            reading_markup_signature(
                turn,
                self.player.position,
                lambda speaker: self.meeting.speaker_for(speaker).name,
                dark=Adw.StyleManager.get_default().get_dark(),
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
        if not self._can_listen:
            return False
        self.player.seek(position)
        self.player.play()
        return True

    def listen_from_row(self, row_turn, origin):
        index = self._row_listening.get(row_turn.index)
        if index is not None and self._can_listen:
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
        GLib.idle_add(lambda: (self.player.seek(word.start_ms), False)[1])
        return True

    def update(self, meeting):
        self._updating_transcript = True
        self._updating_rows = True
        self.meeting = meeting
        layout = getattr(meeting.turns, "layout", meeting.turns) if self._exact else meeting.turns
        self.headline.set_title(meeting.title)
        self.window.set_title(meeting.title)
        self.headline.set_subtitle(
            fmt.preview_subtitle(
                meeting.started_at_ms,
                meeting.duration_ms,
                now_ms=self.window.now_ms,
                tz=self.window.tz,
                received=meeting.origin == "received",
            )
        )
        self.total.set_label(fmt.position(meeting.duration_ms or 0))
        writing = getattr(self.window.get_application(), "writing_available", True)
        self.write_button.set_visible(writing and meeting.can_write_down and meeting.transcript == "none")
        self.seek.set_range(0, max(1, meeting.duration_ms or 1))
        if not self.window._playback_failed and self.window._loaded_key != meeting.key:
            self.window._ensure_player(meeting)
        can_play = (
            meeting.has_audio
            and self.player.loaded
            and self.window._loaded_key == meeting.key
            and not self.window._playback_failed
        )
        for button in self.transport_buttons:
            button.set_sensitive(can_play)
        self.seek.set_sensitive(can_play)
        self._can_listen = can_play
        if self._turns != layout or self._listening_turns is not meeting.listening_turns:
            self._index_listening_rows(meeting, layout)
        if not can_play and self.window.listen.active:
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
        dark = self._style_manager.get_dark()
        matches = frozenset(
            s.id
            for s in meeting.speakers
            if meeting.metadata_editable
            and self.window.get_application().voice_store.has_match(meeting.directory, s.id)
        )
        self._queue_transcript_headers(
            (dark, matches, meeting.speakers, meeting.metadata.get("speakerMerges"), language())
        )
        signature = (
            undo,
            meeting.metadata.get("speakerMerges"),
            self.window.get_application().meeting_trash.allowed(meeting, self.window),
            matches,
            # Library scans reparse the same words. Identity would rebuild the
            # rows and unrealize a button during GTK's delayed activation,
            # cancelling its pending click (and losing keyboard focus).
            meeting.words,
            meeting.turns,
            meeting.speakers,
            meeting.transcript,
            meeting.eta_minutes,
            meeting.error,
            meeting.publication_notice,
            meeting.metadata_editable,
            meeting.has_audio,
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
        self.more_menu.append(text("details.menu"), "win.recording-details")
        self.more_menu.append(text("common.exportEllipsis"), "win.export")
        if undo:
            self.more_menu.append(text("meeting.undoMerge"), "win.undo-speaker-merge")
        if self.window.get_application().meeting_trash.allowed(meeting, self.window) or (
            meeting.published_file is not None and not meeting.metadata_editable
        ):
            section = Gio.Menu()
            from .meeting_trash import menu_item

            section.append_item(menu_item())
            self.more_menu.append_section(None, section)
        while child := self.speakers.get_first_child():
            self.speakers.remove(child)
        self.speakers.append(label(text("common.speakers"), "small", "heading", "dim-label", xalign=0))
        shares = speaking_shares(meeting.words, [s.id for s in meeting.speakers])
        self._speaker_rows.clear()
        for speaker in meeting.speakers:
            self.speakers.append(self._speaker_row(speaker, shares[speaker.id], matches))
        self._updating_rows = False
        self._finish_merge_transition()
        self._listening_changed()
        if meeting.speakers and (meeting.metadata_editable or meeting.published_file is not None):
            self.name_speakers_button = Gtk.Button(
                label=text("meeting.nameSpeakers"), sensitive=meeting.metadata_editable
            )
            self.name_speakers_button.connect("clicked", lambda *_: self.name_speakers())
            self.speakers.append(self.name_speakers_button)
        while child := self.lines.get_first_child():
            self.lines.remove(child)
        publication = self.window.publication_line(meeting)
        if publication:
            self.lines.append(publication)
        trash_error = self.window.trash_error_line(meeting)
        if trash_error:
            self.lines.append(trash_error)
        elif meeting.transcript in ("queued", "running"):
            progress = Gtk.Box(spacing=12, css_classes=["inline-line", "info-line"])
            progress.append(Gtk.Spinner(spinning=True, accessible_role=Gtk.AccessibleRole.PRESENTATION))
            progress.append(label(fmt.eta(meeting.eta_minutes), xalign=0, wrap=True, hexpand=True))
            self.lines.append(progress)
        elif meeting.transcript == "damaged":
            self.lines.append(InlineLine("errors.transcriptDamaged.line"))
        elif meeting.transcript == "failed":
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
        elif meeting.no_speech:
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
        # Only changed timed text replaces transcript items. Metadata/theme
        # updates preserve their focused widgets and refresh headers in batches.
        if self._turns is not layout:
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
            not row.get_mapped()
            or not row.get_realized()
            or row.get_width() <= 0
            or (play.get_visible() and (not play.get_mapped() or not play.get_realized() or play.get_width() <= 0))
            for row, play, _when in self._speaker_rows.values()
        ):
            return GLib.SOURCE_CONTINUE
        self._rows_rebuilt_tick = 0
        self.emit("rows-rebuilt")
        self._queue_dialog_focus()
        return GLib.SOURCE_REMOVE

    def _speaker_row(self, speaker, share, matches):
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
        row = Gtk.Overlay(css_classes=["speaker-row"])
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self._scroll_speaker(row))
        row.add_controller(focus)
        body = box(4)
        top = Gtk.Box(spacing=8)
        dot = label("●", accessible_role=Gtk.AccessibleRole.PRESENTATION)
        dot.set_markup(f'<span foreground="{self._colour(speaker)}">●</span>')
        top.append(dot)
        top.append(
            label(speaker.name, "heading", xalign=0, hexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        )
        top.append(
            label(
                text("meeting.speakerShare", percent=share) if share else text("meeting.speakerShare.underOne"),
                "small",
                "dim-label",
            )
        )
        playable = bool(turns and self.meeting.has_audio)
        if playable:
            top.append(Gtk.Box(width_request=34, height_request=34))
        body.append(top)
        if when or provenance:
            caption_row = Gtk.Box(css_classes=["small", "dim-label", "speaker-caption"], margin_start=18)
            caption_row.set_tooltip_text(
                text("speakers.row.caption", provenance=provenance, when=when)
                if provenance and when
                else provenance or when
            )
            if provenance:
                caption_row.append(label(provenance, xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True))
            if when:
                caption_row.append(
                    label(text("speakers.row.caption", provenance="", when=when) if provenance else when, xalign=0)
                )
            body.append(caption_row)
        if self.meeting.metadata_editable:
            edit = Gtk.Button(child=body, css_classes=["flat", "speaker-edit"])
            edit.update_property(
                [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                [description, text("meeting.nameSpeakers")],
            )
            edit.connect("clicked", lambda *_: self.name_speakers(speaker.id))
            row.set_child(edit)
        else:
            body.update_property([Gtk.AccessibleProperty.LABEL], [description])
            row.set_child(body)
        # Sibling of the activatable row: Play never propagates to its button.
        play = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            css_classes=["circular", "flat"],
            width_request=34,
            height_request=34,
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
            visible=playable,
        )
        play.connect("clicked", lambda *_: self._listen_speaker(speaker.id))
        row.add_overlay(play)
        self._speaker_rows[speaker.id] = row, play, when
        return row

    def stop_listening(self):
        speaker = self.window.listen.speaker
        self.window.listen.stop()
        origin = self._listen_origin
        if (origin is None or not origin.get_mapped()) and speaker in self._speaker_rows:
            origin = self._speaker_rows[speaker][1]
        if origin is not None and origin.get_mapped():
            origin.grab_focus()

    def _listen_speaker(self, identifier):
        self._listen_origin = self._speaker_rows[identifier][1]
        listen = self.window.listen
        if listen.active and listen.speaker == identifier:
            self.player.pause()
            listen.stop()
        else:
            listen.start(identifier)
            if self.split.get_collapsed():
                self.split.set_show_content(True)

    def _listening_changed(self, *_):
        for _turn, _words, row in self.bound.values():
            headers = row.get_first_child()
            headers.get_first_child().set_sensitive(self._can_listen)
            headers.get_last_child().set_sensitive(self._can_listen)
        listen = self.window.listen
        names = {speaker.id: speaker.name for speaker in self.meeting.speakers}
        for identifier, (row, play, when) in self._speaker_rows.items():
            active = listen.active and listen.speaker == identifier
            (row.add_css_class if active else row.remove_css_class)("listening")
            play.set_sensitive(self._can_listen)
            play.set_visible(self.meeting.has_audio and bool(listen.turns_of(identifier)))
            play.set_icon_name("media-playback-stop-symbolic" if active else "media-playback-start-symbolic")
            play.set_tooltip_text(text("common.stop") if active else text("speakers.listen"))
            play.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [
                    text("common.stop")
                    if active
                    else text("speakers.listen.a11y", name=names.get(identifier, ""), when=when)
                ],
            )
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
            # Include the pane's margins in the adjustment's coordinate space.
            adjustment = self.speakers_scroller.get_vadjustment()
            top = adjustment.get_value() + bounds.get_y()
            adjustment.clamp_page(top, top + bounds.get_height())
        return GLib.SOURCE_REMOVE

    def name_speakers(self, identifier=None, *, from_listening=False):
        if not self.meeting.metadata_editable or not self.meeting.speakers:
            return
        from .speakers_dialog import SpeakersDialog

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
            if self.split.get_collapsed():
                self.split.set_show_content(False)
            if source in self._speaker_rows or self._rows_rebuilt_tick or self._merge_transition:
                return GLib.SOURCE_REMOVE
            if destination not in self._speaker_rows:
                return GLib.SOURCE_REMOVE
            row, play, _when = self._speaker_rows[destination]
            target = row.get_child() if isinstance(row.get_child(), Gtk.Button) else play
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
        self.window.toasts.add_toast(
            Adw.Toast(
                title=GLib.markup_escape_text(text("speakers.mergedToast", name=name)),
                button_label=text("common.undo"),
                action_name="win.undo-speaker-merge",
            )
        )

    def merge_speaker(self, source, target):
        if not self.meeting.metadata_editable or self._speaker_future is not None or self.window.monitor._closed:
            return
        from .speaker_edits import merge_into

        transition = self.prepare_speaker_merge(source, target)
        directory = self.meeting.directory
        self._speaker_future = self.window.monitor._executor.submit(
            merge_into, directory, source, target, scanner=self.window.monitor.scanner
        )
        self.listening_bar.update()
        self._speaker_future.add_done_callback(
            lambda f: GLib.idle_add(self._speaker_merged, f, source, target, transition)
        )

    def _speaker_merged(self, future, source, target, transition):
        from concurrent.futures import CancelledError

        self._speaker_future = None
        try:
            future.result()
        except CancelledError:
            return False
        except (OSError, ValueError):
            if not self._closed:
                self.window.toast(text("speakers.changeFailed"))
        else:
            self.window.get_application().recompute_voices(self.meeting.directory, (source, target))
            if not self._closed:
                self.speaker_merged(source, target, transition)
            if not self.window.monitor._closed:
                self.window.monitor.refresh_now()
        if not self._closed:
            self.listening_bar.update()
        return False

    def undo_speaker_merge(self):
        if self._closed or self._speaker_future is not None or not self.meeting.metadata_editable:
            return
        from .library_edits import transaction
        from .meeting_store import read_metadata
        from .speaker_edits import undo_merge

        directory = self.meeting.directory
        self.window.lookup_action("undo-speaker-merge").set_enabled(False)
        self._undo_voice_app = self.window.get_application()

        def undo():
            with transaction(directory):
                history = read_metadata(directory).get("speakerMerges", [])
                undo_merge(directory, scanner=self.window.monitor.scanner)
                affected = (history[-1]["from"], history[-1]["into"]) if history else ()
                return directory, affected

        self._speaker_future = self.window.monitor._executor.submit(undo)
        self._speaker_future.add_done_callback(lambda f: GLib.idle_add(self._merge_undone, f))

    def _merge_undone(self, future):
        from concurrent.futures import CancelledError

        self._speaker_future = None
        try:
            directory, affected = future.result()
        except CancelledError:
            pass
        except (OSError, ValueError):
            if not self._closed:
                self.window.toasts.add_toast(
                    Adw.Toast(
                        title=GLib.markup_escape_text(text("speakers.changeFailed")),
                        button_label=text("common.tryAgain"),
                        action_name="win.undo-speaker-merge",
                    )
                )
                self.window.lookup_action("undo-speaker-merge").set_enabled(True)
        else:
            if affected:
                self._undo_voice_app.recompute_voices(directory, affected)
            if not self.window.monitor._closed:
                self.window.monitor.refresh_now()
        return False

    def _position(self, _player=None, property=None):
        self.listening_bar.update_navigation()
        self.timeline.position_changed()
        index = self._follow_rows.at(self.player.position)
        listen = self.window.listen
        turn = listen.turn
        positions = self._turn_rows.get(turn.index, ()) if turn else ()
        if positions and listen._current(self.player.position) is not None:
            index = max(min(positions), min(index, max(positions)))
        explicit_seek = property is not None and property.name == "position" and not self.player.playing
        resumed = self.player.playing and not getattr(self, "_was_playing", False)
        self._was_playing = self.player.playing
        if (
            not self._updating_transcript
            and index >= 0
            and GLib.get_monotonic_time() >= self._follow_until
            and (self.player.playing or explicit_seek)
            and (index != self._reading_index or explicit_seek or resumed)
        ):
            self._reading_index = index
            self.list.scroll_to(index, Gtk.ListScrollFlags.NONE, None)
        self._updating = True
        self.elapsed.set_label(fmt.position(self.player.position))
        self.seek.set_value(self.player.position)
        self._updating = False
        self.play.set_icon_name(
            "media-playback-pause-symbolic" if self.player.playing else "media-playback-start-symbolic"
        )
        play_name = text("player.pause" if self.player.playing else "common.play")
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
        self.play.set_sensitive(False)
        self._can_listen = False
        self.window.listen.stop()
        self._listening_changed()

    def close(self):
        self._closed = True
        if self._header_source:
            GLib.source_remove(self._header_source)
            self._header_source = 0
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
        self._clear_style_refresh()
        for controller, handler in self._touch_scroll_signals:
            controller.disconnect(handler)
        self._touch_scroll_signals.clear()
        if self._exact:
            self.list.close()
        if self.capture._controls in self.window._capture_headers:
            self.window._capture_headers.remove(self.capture._controls)
        for handler in self._signals:
            self.player.disconnect(handler)
        self._signals.clear()
        for handler in self._listen_signals:
            self.window.listen.disconnect(handler)
        self._listen_signals.clear()
