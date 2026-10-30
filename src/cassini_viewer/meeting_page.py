# SPDX-License-Identifier: GPL-3.0-or-later
"""Pushed Meeting page, with virtualised timed transcript items and transport."""

from bisect import bisect_right

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from . import ux_formats as fmt
from .inline_line import InlineLine
from .meeting_text import COLOURS, DARK_COLOURS, item_markup, speaking_shares
from .strings import text


def box(spacing=0, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def label(value, *classes, **kwargs):
    return Gtk.Label(label=value, css_classes=list(classes), **kwargs)


class TurnItem(GObject.Object):
    def __init__(self, turn):
        super().__init__()
        self.turn = turn


class MeetingPage(Adw.NavigationPage):
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
        self._starts = []
        self._turns = ()
        self._theme = None
        self._speaker_signature = None
        self._match_signature = None
        self.settings = Gio.Settings.new(window.get_application().settings_schema)
        outer = box()
        self.set_child(outer)
        split = Adw.NavigationSplitView(
            min_sidebar_width=260, max_sidebar_width=260, sidebar_width_fraction=0.23, vexpand=True
        )
        outer.append(split)
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
        sidebar.set_content(self.speakers_scroller)
        split.set_sidebar(Adw.NavigationPage.new(sidebar, text("common.speakers")))
        content = Adw.ToolbarView()
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            decoration_layout=":close",
            css_classes=["library-header"],
        )
        self.headline = Adw.WindowTitle()
        header.set_title_widget(self.headline)
        self.export_button = Gtk.Button(label=text("common.exportEllipsis"), action_name="win.export")
        menu = self.more_menu = Gio.Menu()
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
        factory = Gtk.SignalListItemFactory()
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        self.list = Gtk.ListView(
            model=Gtk.NoSelection.new(self.store), factory=factory, css_classes=["meeting-transcript"]
        )
        self.list.set_enable_rubberband(False)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(self.list)
        center.append(self.scroller)
        content.set_content(center)
        split.set_content(Adw.NavigationPage.new(content, meeting.title))
        self._companion = self._companion_card()
        self._build_player(outer)
        self._style_manager = Adw.StyleManager.get_default()
        self._style_signal = self._style_manager.connect("notify::dark", lambda *_: self.update(self.meeting))
        self._signals = [
            self.player.connect("notify::" + name, self._position) for name in ("position", "playing", "duration")
        ]
        self._signals.append(self.player.connect("error", self._error))
        self.update(meeting)
        self._position()

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
        setup = Gtk.Button(label=text("meeting.companionCard.setUp"), css_classes=["suggested-action"])
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
        bar.append(self.seek)
        self.total = label(fmt.position(self.meeting.duration_ms or 0), "small", "dim-label")
        bar.append(self.total)
        # Rate menu copy is undrawn; display the actual rate without a dead click.
        bar.append(label(text("format.playbackRate", rate=f"{self.player.rate:g}"), "small", "badge", "badge-grey"))
        outer.append(bar)

    def _seek(self, _scale, _scroll, value):
        if not self._updating:
            self.player.seek(int(value))
        return True

    def _colour(self, speaker):
        palette = DARK_COLOURS if Adw.StyleManager.get_default().get_dark() else COLOURS
        return palette[speaker.colour_index % len(palette)]

    def _bind(self, _factory, item):
        turn = item.get_item().turn
        if turn is None:
            item.set_child(self._companion)
            return
        speaker = next(s for s in self.meeting.speakers if s.id == turn.speaker)
        row = Gtk.Box(spacing=16, margin_bottom=24)
        names = box(5, width_request=120, valign=Gtk.Align.START)
        name = label("", "heading", xalign=0, wrap=True)
        name.set_markup(f'<span foreground="{self._colour(speaker)}">{GLib.markup_escape_text(speaker.name)}</span>')
        names.append(name)
        if speaker.provenance == "recognised" and self.window.get_application().voice_store.has_match(
            self.meeting.directory, speaker.id
        ):
            tag = label(
                text("common.autoTag"), "auto-tag", halign=Gtk.Align.START, tooltip_text=text("common.autoTag.tooltip")
            )
            tag.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text("common.autoTag.tooltip")])
            names.append(tag)
        names.append(label(fmt.position(turn.start_ms), "small", "dim-label", xalign=0))
        words = Gtk.Label(
            xalign=0,
            yalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            hexpand=True,
            use_markup=True,
            css_classes=["words"],
        )
        words.set_markup(item_markup(turn.words, self.player.position))
        words.connect("activate-link", lambda _label, uri: self._word_clicked(turn, uri))
        row.append(names)
        row.append(words)
        item.set_child(row)
        self.bound[turn.index] = (turn, words, row)

    def _unbind(self, _factory, item):
        turn = item.get_item().turn if item.get_item() else None
        if turn is not None:
            self.bound.pop(turn.index, None)
        item.set_child(None)

    def _word_clicked(self, turn, uri):
        try:
            index = int(uri.removeprefix("item:"))
            word = turn.words[index]
        except (ValueError, IndexError):
            return True
        GLib.idle_add(lambda: (self.player.seek(word.start_ms), False)[1])
        return True

    def update(self, meeting):
        self.meeting = meeting
        self.headline.set_title(meeting.title)
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
        source = self.window._activity_source
        self.set_connected(source.connected if source else False)
        if self.speakers_dialog and not self.speakers_dialog.closed:
            self.speakers_dialog.update(meeting)
        undo = bool(meeting.metadata_editable and meeting.metadata.get("speakerMerges"))
        self.window.lookup_action("undo-speaker-merge").set_enabled(undo and self._speaker_future is None)
        self.more_menu.remove_all()
        self.more_menu.append(text("common.exportEllipsis"), "win.export")
        if undo:
            self.more_menu.append(text("meeting.undoMerge"), "win.undo-speaker-merge")
        if self.window.get_application().meeting_trash.allowed(meeting, self.window):
            section = Gio.Menu()
            from .meeting_trash import menu_item

            section.append_item(menu_item())
            self.more_menu.append_section(None, section)
        dark = self._style_manager.get_dark()
        matches = frozenset(
            s.id
            for s in meeting.speakers
            if self.window.get_application().voice_store.has_match(meeting.directory, s.id)
        )
        signature = (
            matches,
            id(meeting.words),
            meeting.speakers,
            meeting.transcript,
            meeting.eta_minutes,
            meeting.error,
            meeting.metadata_editable,
            dark,
        )
        if signature == self._signature:
            return
        self._signature = signature
        while child := self.speakers.get_first_child():
            self.speakers.remove(child)
        self.speakers.append(label(text("common.speakers"), "small", "heading", "dim-label", xalign=0))
        shares = speaking_shares(meeting.words, [s.id for s in meeting.speakers])
        for speaker in meeting.speakers:
            row = box(4)
            top = Gtk.Box(spacing=8)
            dot = label("●", accessible_role=Gtk.AccessibleRole.PRESENTATION)
            dot.set_markup(f'<span foreground="{self._colour(speaker)}">●</span>')
            top.append(dot)
            top.append(label(speaker.name, "heading", xalign=0, hexpand=True, wrap=True))
            top.append(label(text("meeting.speakerShare", percent=shares[speaker.id]), "small", "dim-label"))
            row.append(top)
            caption = {"user": "namedByYou", "recognised": "recognised"}.get(speaker.provenance, "notNamed")
            caption_text = (
                (text("common.yourMicrophone") if speaker.id == "me" else text("recordFirst.sound.title"))
                if speaker.provenance == "source"
                else text("meeting.speakerCaption." + caption)
            )
            if speaker.provenance != "recognised" or speaker.id in matches:
                row.append(
                    label(
                        caption_text,
                        "small",
                        "dim-label",
                        xalign=0,
                        wrap=True,
                        margin_start=18,
                    )
                )
            if meeting.metadata_editable:
                edit = Gtk.Button(child=row, css_classes=["flat", "speaker-edit"])
                edit.update_property(
                    [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                    [speaker.name, text("meeting.nameSpeakers")],
                )
                edit.connect("clicked", lambda _button, identifier=speaker.id: self.name_speakers(identifier))
                self.speakers.append(edit)
            else:
                self.speakers.append(row)
        if meeting.speakers and meeting.metadata_editable:
            self.name_speakers_button = Gtk.Button(label=text("meeting.nameSpeakers"))
            self.name_speakers_button.connect("clicked", lambda *_: self.name_speakers())
            self.speakers.append(self.name_speakers_button)
        while child := self.lines.get_first_child():
            self.lines.remove(child)
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
        if not self.store.get_n_items():
            self.store.append(TurnItem(None))
        common = 0
        if self._theme == dark and self._speaker_signature == meeting.speakers and self._match_signature == matches:
            for before, after in zip(self._turns, meeting.turns, strict=False):
                if before != after:
                    break
                common += 1
        removed = len(self._turns) - common
        additions = [TurnItem(turn) for turn in meeting.turns[common:]]
        if removed or additions:
            self.store.splice(common + 1, removed, additions)
        self._turns = meeting.turns
        self._theme = dark
        self._speaker_signature = meeting.speakers
        self._match_signature = matches
        self._starts = [turn.start_ms for turn in meeting.turns]
        self._position()

    def name_speakers(self, identifier=None):
        if not self.meeting.metadata_editable or not self.meeting.speakers:
            return
        from .speakers_dialog import SpeakersDialog

        if self.speakers_dialog and not self.speakers_dialog.closed:
            self.speakers_dialog.close()
        visible = {s.id for s in self.meeting.speakers}
        selected = identifier or self._selected_speaker
        self._selected_speaker = selected if selected in visible else self.meeting.speakers[0].id
        self.speakers_dialog = SpeakersDialog(self, self._selected_speaker)
        self.speakers_dialog.present(self.window)
        self.speakers_dialog.entry.grab_focus()

    def undo_speaker_merge(self):
        if self._closed or self._speaker_future is not None or not self.meeting.metadata_editable:
            return
        from .meeting_store import LOCK, read_metadata
        from .speaker_edits import undo_merge

        directory = self.meeting.directory
        self.window.lookup_action("undo-speaker-merge").set_enabled(False)
        self._undo_voice_app = self.window.get_application()

        def undo():
            with LOCK:
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
        index = bisect_right(self._starts, self.player.position) - 1
        explicit_seek = property is not None and property.name == "position" and not self.player.playing
        resumed = self.player.playing and not getattr(self, "_was_playing", False)
        self._was_playing = self.player.playing
        if (
            index >= 0
            and (self.player.playing or explicit_seek)
            and (index != self._reading_index or explicit_seek or resumed)
        ):
            self._reading_index = index
            self.list.scroll_to(index + 1, Gtk.ListScrollFlags.NONE, None)
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
        for turn, words, _row in list(self.bound.values()):
            words.set_markup(item_markup(turn.words, self.player.position))

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

    def close(self):
        self._closed = True
        if self.speakers_dialog and not self.speakers_dialog.closed:
            self.speakers_dialog.close()
        if self._speaker_future is not None:
            self._speaker_future.cancel()
        if self._style_signal:
            self._style_manager.disconnect(self._style_signal)
            self._style_signal = 0
        if self.capture._controls in self.window._capture_headers:
            self.window._capture_headers.remove(self.capture._controls)
        for handler in self._signals:
            self.player.disconnect(handler)
        self._signals.clear()
