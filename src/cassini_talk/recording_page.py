# SPDX-License-Identifier: GPL-3.0-or-later
"""Live recording controls and app-owned title edits."""

import logging

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import ux_formats as fmt
from .inline_line import InlineLine
from .library_window import label, vertical
from .spectrogram_strip import SpectrogramStrip
from .strings import text


class _StopContent(Gtk.Box):
    def __init__(self, initial_label):
        super().__init__(spacing=7, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.append(
            Gtk.Box(
                width_request=9,
                height_request=9,
                valign=Gtk.Align.CENTER,
                css_classes=["recording-stop-glyph"],
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        self._label = Gtk.Label(label=initial_label)
        self.append(self._label)

    def get_label(self):
        return self._label.get_label()

    def set_label(self, value):
        self._label.set_label(value)


class _TitleEntry(Gtk.Entry):
    def __init__(self, page, **kwargs):
        super().__init__(**kwargs)
        self._page = page

    def grab_focus(self):
        if self._page.title_stack.get_visible_child_name() != "edit":
            self._page.title_stack.set_visible_child_name("edit")
        return super().grab_focus()


class RecordingPage(Adw.NavigationPage):
    def __init__(self, window, meeting):
        super().__init__(title=meeting.title)
        self.window, self.meeting = window, meeting
        self.app = window.get_application()
        draft = self.app.comfort.draft(meeting)
        self._save_timer = 0
        self._saving = 0
        self._closed = False
        self._title_dirty = False
        self._updating_title = False
        self._line_signature = None
        self._scroll_source = 0
        self._split_collapsed_handler = 0
        self.sources_dialog = None
        self._dirty = False

        outer = vertical(vexpand=True, hexpand=True, css_classes=["recording-page"])
        responsive = Adw.BreakpointBin(child=outer, width_request=320, height_request=100)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.connect("apply", lambda *_: outer.add_css_class("narrow"))
        narrow.connect("unapply", lambda *_: outer.remove_css_class("narrow"))
        responsive.add_breakpoint(narrow)
        self.set_child(responsive)

        header = self.top_bar = Adw.HeaderBar(
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
            action_name="win.back-library",
            css_classes=["flat", "meeting-icon-btn"],
            valign=Gtk.Align.CENTER,
        )
        self.back_button.update_property([Gtk.AccessibleProperty.LABEL], [text("common.backToLibrary")])
        header.pack_start(self.back_button)
        if window._external_meeting or not hasattr(window, "capture_header"):
            self.capture = window.capture_controls(meeting)
            self.capture.set_tooltip_text(text("recording.pill.noSleep"))
            header.pack_start(self.capture)
        else:
            self.capture = window.capture_header

        menu = self.more_menu = Gio.Menu()
        menu.append(text("details.menu"), "win.recording-details")
        self.more_button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text=text("meeting.moreActions"),
            menu_model=menu,
            css_classes=["flat", "meeting-icon-btn"],
            valign=Gtk.Align.CENTER,
        )
        self.more_button.get_popover().set_halign(Gtk.Align.END)
        self.more_button.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.moreActions")])
        header.pack_end(self.more_button)
        outer.append(header)

        self.full_header = vertical(6, css_classes=["meeting-full-header"])
        self.title_stack = Gtk.Stack(
            hhomogeneous=False, vhomogeneous=False, transition_type=Gtk.StackTransitionType.NONE
        )
        title_row = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.headline = label(
            draft.get("title") or meeting.title,
            "meeting-title",
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        self.set_title(self.headline.get_label())
        title_row.append(self.headline)
        self.rename_button = Gtk.Button(
            icon_name="document-edit-symbolic",
            tooltip_text=text("meeting.rename.confirm"),
            css_classes=["flat", "circular", "meeting-rename-btn"],
            valign=Gtk.Align.CENTER,
        )
        self.rename_button.update_property([Gtk.AccessibleProperty.LABEL], [text("meeting.rename.confirm")])
        self.rename_button.connect("clicked", lambda *_: self.start_rename())
        title_row.append(self.rename_button)
        self.title_stack.add_named(title_row, "view")

        self.title_entry = _TitleEntry(
            self,
            text=self._editable_title(meeting, draft),
            placeholder_text=text("library.untitled"),
            max_length=500,
            hexpand=True,
            css_classes=["recording-title"],
        )
        self.title_entry.update_property([Gtk.AccessibleProperty.LABEL], [text("recording.title.label")])
        window.protect_text_input(self.title_entry)
        self.title_entry.connect("changed", self._title_changed)
        self.title_entry.connect("activate", lambda *_: self.finish_rename())
        entry_focus = Gtk.EventControllerFocus()
        entry_focus.connect("leave", self._on_title_focus_leave)
        self.title_entry.add_controller(entry_focus)
        entry_keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        entry_keys.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.KeyvalTrigger.new(Gdk.KEY_Escape, Gdk.ModifierType(0)),
                Gtk.CallbackAction.new(lambda *_: (self.finish_rename(), True)[1]),
            )
        )
        self.title_entry.add_controller(entry_keys)
        self.title_stack.add_named(self.title_entry, "edit")
        self.title_stack.set_visible_child_name("view")
        self.full_header.append(self.title_stack)

        self.subtitle_label = label(self._subtitle_text(meeting), "meeting-subtitle", xalign=0, wrap=True)
        self.full_header.append(self.subtitle_label)
        outer.append(self.full_header)

        rename_shortcut = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        rename_shortcut.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.KeyvalTrigger.new(Gdk.KEY_F2, Gdk.ModifierType(0)),
                Gtk.CallbackAction.new(self._on_rename_shortcut),
            )
        )
        self.add_controller(rename_shortcut)

        column = vertical(20, css_classes=["recording-body"])
        self.scroller = Gtk.ScrolledWindow(child=column, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._vadj = self.scroller.get_vadjustment()
        self._vadj_handler = self._vadj.connect("value-changed", self._queue_scrolled)
        outer.append(self.scroller)

        live = Gtk.Box(spacing=16, valign=Gtk.Align.CENTER)
        timer_group = Gtk.Box(spacing=14, valign=Gtk.Align.CENTER)
        self.dot = Gtk.Box(
            width_request=12,
            height_request=12,
            valign=Gtk.Align.CENTER,
            margin_start=6,
            css_classes=["recording-dot"],
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        timer_group.append(self.dot)
        self.timer = label("", "recording-timer", xalign=0)
        timer_group.append(self.timer)
        live.append(timer_group)

        self.stop_content = _StopContent(text("common.stop"))
        self.saving_spinner = Gtk.Spinner(spinning=False, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.saving_label = Gtk.Label(label=text("states.badge.saving"))
        self.saving_content = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.saving_content.append(self.saving_spinner)
        self.saving_content.append(self.saving_label)
        self.stop = Gtk.Button(
            child=self.stop_content, css_classes=["cassini-record", "recording-stop"], valign=Gtk.Align.CENTER
        )
        self.stop.update_property([Gtk.AccessibleProperty.LABEL], [text("common.stop")])
        self.stop.connect("clicked", lambda *_: window.stop_meeting(self.meeting))
        live.append(self.stop)
        column.append(live)

        self.lines = vertical(visible=False)
        column.append(self.lines)

        sources = vertical(css_classes=["recording-sources"])
        self.mic_row = Gtk.Box(spacing=14, valign=Gtk.Align.CENTER, css_classes=["recording-source"])
        mic_labels = vertical(2, hexpand=True, valign=Gtk.Align.CENTER)
        mic_labels.append(
            label(text("common.yourMicrophone"), "recording-source-title", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        )
        self.mic_warning = label(
            text("errors.micSilentRow.line"),
            "small",
            "recording-source-warning",
            xalign=0,
            wrap=True,
            visible=False,
        )
        mic_labels.append(self.mic_warning)
        self.mic_row.append(mic_labels)
        self.mic_level = SpectrogramStrip(kind="row", lanes=("me",))
        self.mic_row.append(self.mic_level)
        self.check = Gtk.Button(
            label=text("errors.micSilentRow.action"),
            valign=Gtk.Align.CENTER,
            visible=False,
            css_classes=["spectro-check"],
        )
        self.check.connect("clicked", self._check_sound)
        self.mic_row.append(self.check)
        sources.append(self.mic_row)

        self.source_separator = Gtk.Separator(css_classes=["recording-source-sep"])
        sources.append(self.source_separator)

        others = self.others_row = Gtk.Box(spacing=14, valign=Gtk.Align.CENTER, css_classes=["recording-source"])
        others_labels = vertical(2, hexpand=True, valign=Gtk.Align.CENTER)
        others_labels.append(
            label(text("common.theOthers"), "recording-source-title", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        )
        self.others_caption = label("", "small", "dim-label", xalign=0, wrap=True)
        others_labels.append(self.others_caption)
        others.append(others_labels)
        self.others_level = SpectrogramStrip(kind="row", lanes=("others",))
        others.append(self.others_level)
        sources.append(others)

        self.change_separator = Gtk.Separator(css_classes=["recording-source-sep"])
        sources.append(self.change_separator)

        change_box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        change_box.append(
            Gtk.Image(
                icon_name="audio-input-microphone-symbolic",
                pixel_size=16,
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
                css_classes=["recording-change-icon"],
            )
        )
        change_box.append(
            label(text("recording.changeWhatIsRecorded"), "recording-change-label", xalign=0, hexpand=True)
        )
        change_box.append(
            Gtk.Image(
                icon_name="go-next-symbolic",
                pixel_size=14,
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
                css_classes=["recording-change-chevron"],
            )
        )
        self.change_sources = Gtk.Button(
            child=change_box,
            hexpand=True,
            css_classes=["flat", "recording-change-row"],
        )
        self.change_sources.update_property([Gtk.AccessibleProperty.LABEL], [text("recording.changeWhatIsRecorded")])
        self.change_sources.connect("clicked", self._change_sources)
        sources.append(self.change_sources)
        column.append(sources)

        if hasattr(window, "split") and window.split is not None:
            self._split_collapsed_handler = window.split.connect("notify::collapsed", self._sync_back_button)
        self._sync_back_button()
        self.update_status()

    def _subtitle_text(self, meeting):
        return fmt.preview_subtitle(meeting.started_at_ms, None, now_ms=self.window.now_ms, tz=self.window.tz)

    def _sync_back_button(self, *_):
        collapsed = bool(
            hasattr(self.window, "split") and self.window.split is not None and self.window.split.get_collapsed()
        )
        self.back_button.set_visible(bool(self.window._external_meeting or collapsed))

    def _queue_scrolled(self, *_):
        if not self._scroll_source and not self._closed:
            self._scroll_source = GLib.idle_add(self._apply_scrolled)

    def _apply_scrolled(self):
        self._scroll_source = 0
        if not self._closed:
            scrolled = self._vadj.get_value() > 0.5
            (self.top_bar.add_css_class if scrolled else self.top_bar.remove_css_class)("scrolled")
        return GLib.SOURCE_REMOVE

    def _on_rename_shortcut(self, *_):
        if not self.window._text_focused() and self.rename_button.get_sensitive():
            self.start_rename()
            return True
        return False

    def start_rename(self):
        if self._closed or not self.rename_button.get_sensitive():
            return
        self.title_entry.grab_focus()
        self.title_entry.select_region(0, -1)

    def finish_rename(self):
        if self._closed or self.title_stack.get_visible_child_name() != "edit":
            return
        self.title_stack.set_visible_child_name("view")
        if self.rename_button.get_mapped() and self.rename_button.get_sensitive():
            self.rename_button.grab_focus()

    def _on_title_focus_leave(self, *_):
        if not self._closed and self.title_stack.get_visible_child_name() == "edit":
            focus = self.window.get_focus()
            if focus is None or (focus is not self.title_entry and not focus.is_ancestor(self.title_entry)):
                self.title_stack.set_visible_child_name("view")

    def elapsed(self):
        return self.app.comfort.elapsed(self.meeting)

    def _change_sources(self, *_):
        from .what_is_recorded import WhatIsRecorded

        if self.sources_dialog is None or self.sources_dialog.closed:
            self.sources_dialog = WhatIsRecorded(self)
        self.sources_dialog.present(self.window)

    def _check_sound(self, *_):
        # The desktop owns its sound preferences. No change to devices by Cassini.
        try:
            Gio.AppInfo.create_from_commandline("gnome-control-center sound", None, Gio.AppInfoCreateFlags.NONE).launch(
                [], None
            )
        except GLib.Error as error:
            logging.getLogger(__name__).warning("Could not open desktop sound settings: %s", error)

    def _title_changed(self, *_):
        if self._updating_title:
            return
        self._title_dirty = True
        title = self.title_entry.get_text().strip() or self.meeting.title
        self.headline.set_label(title)
        self.headline.set_tooltip_text(title)
        self.set_title(title)
        self._changed()

    def _changed(self):
        self._dirty = True
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(250, self._save)

    def _save(self):
        self._save_timer = 0
        if not self._dirty:
            return False
        self._dirty = False
        changes = {}
        if self._title_dirty:
            changes["title"] = self.title_entry.get_text()
        self._title_dirty = False
        self._saving += 1
        future = self.app.comfort.edit(self.meeting.directory, **changes)
        future.add_done_callback(lambda f: GLib.idle_add(self._saved, f, changes))
        return False

    def _saved(self, future, changes):
        self._saving -= 1
        try:
            future.result()
        except (OSError, ValueError) as error:
            self.app.recording_error = str(error)  # Keep existing behavior until error copy is published.
            self._title_dirty |= "title" in changes
            self._dirty = True
        if not self.window.monitor._closed:
            self.window.monitor.refresh({self.meeting.directory})
        return False

    @staticmethod
    def _editable_title(meeting, draft):
        if "title" in draft:
            return draft["title"] or ""
        return "" if meeting.title_is_fallback else meeting.title

    def update(self, meeting):
        self.meeting = meeting
        if self.sources_dialog is not None:
            self.sources_dialog.update(meeting)
        self.subtitle_label.set_label(self._subtitle_text(meeting))
        draft = self.app.comfort.draft(meeting)
        focus = self.window.get_focus()
        editing = focus is self.title_entry or (focus is not None and focus.is_ancestor(self.title_entry))
        if not self._dirty and not self._saving and not editing:
            title = self._editable_title(meeting, draft)
            if self.title_entry.get_text() != title:
                self._updating_title = True
                self.title_entry.set_text(title)
                self._updating_title = False
            headline = title.strip() or meeting.title
            self.headline.set_label(headline)
            self.headline.set_tooltip_text(headline)
            self.set_title(headline)
        self.update_status()

    def update_status(self):
        status = self.app.comfort.status(self.meeting)
        sources = self.app.comfort.sources(self.meeting)
        self.subtitle_label.set_label(self._subtitle_text(self.meeting))
        self.timer.set_label(fmt.live_big(self.elapsed()))
        from pathlib import Path

        from .capture_guard import recorder_stopping
        from .library import meeting_id

        stopping = self.app._browser_stops
        stopping_ids = {
            identifier for entry in stopping if isinstance(entry, str) and (identifier := meeting_id(Path(entry)))
        }
        local_native = self.app.comfort._native_for(self.meeting)
        is_stopping = (
            self.meeting.lifecycle == "saving"
            or recorder_stopping(local_native)
            or None in stopping
            or str(self.meeting.directory) in stopping
            or self.meeting.id in stopping_ids
        )
        stop_label = text("states.badge.saving" if is_stopping else "common.stop")
        self.stop_content.set_label(stop_label)
        self.saving_label.set_label(stop_label)
        self.saving_spinner.set_spinning(is_stopping)
        target_child = self.saving_content if is_stopping else self.stop_content
        if self.stop.get_child() is not target_child:
            self.stop.set_child(target_child)
        self.stop.update_property([Gtk.AccessibleProperty.LABEL], [stop_label])
        (self.dot.add_css_class if is_stopping else self.dot.remove_css_class)("saving")
        can_act = self.meeting.lifecycle == "recording" and not is_stopping
        self.stop.set_sensitive(can_act)
        self.rename_button.set_sensitive(can_act and self.meeting.metadata_editable)
        self.change_sources.set_sensitive(can_act and str(self.meeting.directory) not in self.app._source_changes)
        self.mic_row.set_visible(sources.me)
        self.others_row.set_visible(sources.others)
        self.source_separator.set_visible(sources.me and sources.others)
        self.change_separator.set_visible(sources.me or sources.others)
        silent = status["silent"]
        self.mic_warning.set_visible(silent)
        self.check.set_visible(silent)
        (self.mic_row.add_css_class if silent else self.mic_row.remove_css_class)("mic-silent")
        for bar, level in zip((self.mic_level, self.others_level), status["levels"], strict=True):
            bar.set_fraction(level)
            self.window.bind_spectrogram(bar, self.meeting, silent=silent, stopping=is_stopping)
        browser = sources.source == "browser"
        self.others_caption.set_label(
            text("recording.others.browserVoices", n=status["voices"]) if browser else text("recordFirst.sound.title")
        )
        refused = tuple(tuple(sorted(line.items())) for line in self.window._refusal_lines(self.meeting))
        signature = (
            status["line"],
            status["minutes"],
            sources.fallback,
            status.get("output_muted", False),
            status.get("source_problem", False),
            refused,
        )
        if signature != self._line_signature:
            self._line_signature = signature
            while child := self.lines.get_first_child():
                self.lines.remove(child)
            self.lines.set_visible(bool(signature[0] or signature[2] or signature[3] or signature[4] or refused))
            if signature[0]:
                self.lines.append(InlineLine(signature[0], n=signature[1]))
            if signature[3]:
                self.lines.append(InlineLine("recording.systemMuted"))
            if signature[4]:
                self.lines.append(InlineLine("recording.sourceProblem"))
            # One line per refused side (R79 §3); the fallback line as before.
            self.window.append_refusal_lines(self.lines, refused, signature[2], self.meeting)

    def flush_edits(self):
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        self._save()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._scroll_source:
            GLib.source_remove(self._scroll_source)
            self._scroll_source = 0
        if self._vadj_handler:
            self._vadj.disconnect(self._vadj_handler)
            self._vadj_handler = 0
        if self._split_collapsed_handler and hasattr(self.window, "split") and self.window.split is not None:
            self.window.split.disconnect(self._split_collapsed_handler)
            self._split_collapsed_handler = 0
        if self.sources_dialog is not None and not self.sources_dialog.closed:
            self.sources_dialog.force_close()
        self.window._editor_focus(self.title_entry, False)
        self.flush_edits()
        for picture in (self.mic_level, self.others_level, self.capture._controls["picture"]):
            self.window.spectrograms.remove(picture)
        if (
            self.capture is not getattr(self.window, "capture_header", None)
            and self.capture._controls in self.window._capture_headers
        ):
            self.window._capture_headers.remove(self.capture._controls)
