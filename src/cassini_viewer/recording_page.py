# SPDX-License-Identifier: GPL-3.0-or-later
"""Live recording controls and app-owned title edits."""

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from . import ux_formats as fmt
from .inline_line import InlineLine
from .library_window import label, vertical
from .strings import text


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
        self.sources_dialog = None
        self._dirty = False
        view = Adw.ToolbarView(css_classes=["recording-page"])
        self.set_child(view)
        header = Adw.HeaderBar(
            show_start_title_buttons=False,
            show_back_button=False,
            decoration_layout=":close",
            css_classes=["library-header"],
        )
        header.pack_start(
            Gtk.Button(
                icon_name="go-previous-symbolic",
                tooltip_text=text("common.backToLibrary"),
                action_name="win.back-library",
                css_classes=["flat"],
            )
        )
        self.capture = window.capture_controls(meeting)
        self.capture.set_tooltip_text(text("recording.pill.noSleep"))
        header.pack_start(self.capture)
        self.headline = label(draft.get("title") or meeting.title, "heading")
        self.set_title(self.headline.get_label())
        header.set_title_widget(self.headline)
        view.add_top_bar(header)
        column = vertical(22, margin_top=32, margin_bottom=32, margin_start=20, margin_end=20)
        column.set_size_request(640, -1)
        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=680, child=column)
        view.set_content(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        live = Gtk.Box(spacing=22)
        dot = Gtk.Box(width_request=22, height_request=22, valign=Gtk.Align.CENTER, css_classes=["recording-dot"])
        live.append(dot)
        timer_box = vertical(0, hexpand=True)
        self.timer = label("", "recording-timer", xalign=0)
        timer_box.append(self.timer)
        started = fmt.row_time(meeting.started_at_ms, tz=window.tz) if meeting.started_at_ms is not None else ""
        timer_box.append(label(text("recording.since", time=started), "dim-label", xalign=0))
        live.append(timer_box)
        self.stop = Gtk.Button(css_classes=["cassini-record", "recording-stop"], valign=Gtk.Align.CENTER)
        self.stop.set_child(Adw.ButtonContent(icon_name="media-playback-stop-symbolic", label=text("common.stop")))
        self.stop.connect("clicked", lambda *_: window.stop_meeting(self.meeting))
        live.append(self.stop)
        column.append(live)
        self.lines = vertical(visible=False)
        column.append(self.lines)
        title_box = vertical(6)
        title_box.append(label(text("recording.title.label"), "small", "heading", "dim-label", xalign=0))
        self.title_entry = Gtk.Entry(
            text=self._editable_title(meeting, draft),
            placeholder_text=text("library.untitled"),
            max_length=500,
            css_classes=["recording-title"],
        )
        self.title_entry.update_property([Gtk.AccessibleProperty.LABEL], [text("recording.title.label")])
        window.protect_text_input(self.title_entry)
        self.title_entry.connect("changed", self._title_changed)
        title_box.append(self.title_entry)
        title_box.append(
            label(
                text("recording.title.caption"),
                "small",
                "dim-label",
                xalign=0,
                visible=meeting.capture_source != "gnome",
            )
        )
        column.append(title_box)
        sources = vertical(css_classes=["recording-sources"])
        self.mic_row = Gtk.Box(spacing=14, css_classes=["recording-source"])
        self.mic_row.append(Gtk.Image(icon_name="audio-input-microphone-symbolic"))
        mic_labels = vertical(2, hexpand=True)
        mic_labels.append(label(text("common.yourMicrophone"), "heading", xalign=0))
        self.mic_warning = label(
            text("errors.micSilentRow.line"), "small", "heading", xalign=0, wrap=True, visible=False
        )
        mic_labels.append(self.mic_warning)
        self.mic_row.append(mic_labels)
        self.mic_level = Gtk.ProgressBar(
            width_request=96, valign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.PRESENTATION
        )
        self.mic_row.append(self.mic_level)
        self.check = Gtk.Button(label=text("errors.micSilentRow.action"), valign=Gtk.Align.CENTER, visible=False)
        self.check.connect("clicked", self._check_sound)
        self.mic_row.append(self.check)
        sources.append(self.mic_row)
        self.source_separator = Gtk.Separator()
        sources.append(self.source_separator)
        others = self.others_row = Gtk.Box(spacing=14, css_classes=["recording-source"])
        others.append(Gtk.Image(icon_name="computer-symbolic"))
        others_labels = vertical(2, hexpand=True)
        others_labels.append(label(text("common.theOthers"), "heading", xalign=0))
        self.others_caption = label("", "small", "dim-label", xalign=0)
        others_labels.append(self.others_caption)
        others.append(others_labels)
        self.others_level = Gtk.ProgressBar(
            width_request=96, valign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.PRESENTATION
        )
        others.append(self.others_level)
        others.append(Gtk.Box(width_request=66))
        sources.append(others)
        sources.append(Gtk.Separator())
        self.change_sources = Gtk.Button(
            label=text("recording.changeWhatIsRecorded"),
            css_classes=["flat", "recording-change"],
            halign=Gtk.Align.START,
        )
        self.change_sources.connect("clicked", self._change_sources)
        source_actions = Gtk.Box(hexpand=True)
        self.change_sources.set_hexpand(True)
        source_actions.append(self.change_sources)
        self.details_button = Gtk.Button(
            label=text("details.link"), css_classes=["flat", "recording-change"], action_name="win.recording-details"
        )
        source_actions.append(self.details_button)
        sources.append(source_actions)
        column.append(sources)
        self.update_status()

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
            self.set_title(headline)
        self.update_status()

    def update_status(self):
        from .what_is_recorded import explain_fallback

        status = self.app.comfort.status(self.meeting)
        sources = self.app.comfort.sources(self.meeting)
        self.timer.set_label(fmt.live_big(self.elapsed()))
        stopping = self.app._browser_stops
        self.stop.set_sensitive(
            self.meeting.lifecycle == "recording"
            and None not in stopping
            and str(self.meeting.directory) not in stopping
        )
        self.change_sources.set_sensitive(
            self.stop.get_sensitive() and str(self.meeting.directory) not in self.app._source_changes
        )
        self.mic_row.set_visible(sources.me)
        self.others_row.set_visible(sources.others)
        self.source_separator.set_visible(sources.me and sources.others)
        silent = status["silent"]
        self.mic_warning.set_visible(silent)
        self.check.set_visible(silent)
        (self.mic_row.add_css_class if silent else self.mic_row.remove_css_class)("mic-silent")
        for bar, level in zip((self.mic_level, self.others_level), status["levels"], strict=True):
            bar.set_fraction(level)
        browser = sources.source == "browser"
        self.others_caption.set_label(
            text("recording.others.browserVoices", n=status["voices"]) if browser else text("recordFirst.sound.title")
        )
        signature = status["line"], status["minutes"], sources.fallback
        if signature != self._line_signature:
            self._line_signature = signature
            while child := self.lines.get_first_child():
                self.lines.remove(child)
            self.lines.set_visible(bool(signature[0] or signature[2]))
            if signature[0]:
                self.lines.append(InlineLine(signature[0], n=signature[1]))
            if signature[2]:
                self.lines.append(
                    InlineLine(
                        "errors.othersFallback.line",
                        action="errors.othersFallback.action",
                        callback=lambda: explain_fallback(self.window),
                        info=True,
                    )
                )

    def flush_edits(self):
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        self._save()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.sources_dialog is not None and not self.sources_dialog.closed:
            self.sources_dialog.force_close()
        self.window._editor_focus(self.title_entry, False)
        self.flush_edits()
        if self.capture._controls in self.window._capture_headers:
            self.window._capture_headers.remove(self.capture._controls)
