# SPDX-License-Identifier: GPL-3.0-or-later
"""UX6: pushed live recording page; app-owned titles and time-pinned notes."""

import re

from gi.repository import Adw, Gio, GLib, Gtk

from . import ux_formats as fmt
from .inline_line import InlineLine
from .library_window import label, vertical
from .recording_details import edit_recording
from .strings import text


class TimedNotes:
    def __init__(self, buffer, notes, elapsed, changed):
        self.buffer, self.elapsed, self.changed = buffer, elapsed, changed
        self.guard = True
        self.marks = []
        self.prefix = buffer.create_tag("note-time", editable=False)
        for note in notes:
            if buffer.get_char_count():
                buffer.insert(buffer.get_end_iter(), "\n")
            self._prefix(buffer.get_end_iter(), note["atMs"])
            buffer.insert(buffer.get_end_iter(), note["text"])
        self.guard = False
        self.insert_signal = buffer.connect("insert-text", self._insert)
        self.change_signal = buffer.connect("changed", lambda *_: changed() if not self.guard else None)

    def _prefix(self, iterator, at):
        self.marks.append((self.buffer.create_mark(None, iterator, True), at))
        self.buffer.insert_with_tags(iterator, fmt.position(at) + "  ", self.prefix)

    def _insert(self, buffer, iterator, value, _length):
        if self.guard:
            return
        buffer.stop_emission_by_name("insert-text")
        self.guard = True
        for index, part in enumerate(value.split("\n")):
            if index:
                buffer.insert(iterator, "\n")
            if part:
                if iterator.starts_line() and iterator.ends_line():
                    self._prefix(iterator, self.elapsed())
                buffer.insert(iterator, part)
        self.guard = False
        self.changed()

    def values(self):
        values, seen = [], set()
        for mark, at in self.marks:
            start = self.buffer.get_iter_at_mark(mark)
            if not start.starts_line() or start.get_offset() in seen:
                continue
            seen.add(start.get_offset())
            end = start.copy()
            end.forward_to_line_end()
            value = self.buffer.get_text(start, end, False)
            match = re.match(r"^\d+:\d{2}:\d{2}  (.*)$", value)
            if match and match[1].strip():
                values.append(dict(atMs=int(at), text=match[1]))
        return values


class RecordingPage(Adw.NavigationPage):
    def __init__(self, window, meeting):
        super().__init__(title=meeting.title)
        self.window, self.meeting = window, meeting
        self.app = window.get_application()
        self._save_timer = 0
        self._closed = False
        self._editing = False
        self._line_signature = None
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
        self.capture = window.capture_controls()
        self.capture.set_tooltip_text(text("recording.pill.noSleep"))
        header.pack_start(self.capture)
        self.headline = label(meeting.title, "heading")
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
        stop_box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        stop_box.append(label("■", accessible_role=Gtk.AccessibleRole.PRESENTATION))
        stop_box.append(label(text("common.stop")))
        self.stop.set_child(stop_box)
        self.stop.connect("clicked", lambda *_: window.stop_meeting(self.meeting))
        live.append(self.stop)
        column.append(live)
        self.lines = vertical(visible=False)
        column.append(self.lines)
        title_box = vertical(6)
        title_box.append(label(text("recording.title.label"), "small", "heading", "dim-label", xalign=0))
        self.title_entry = Gtk.Entry(text=meeting.title, max_length=1000, css_classes=["recording-title"])
        self.title_entry.update_property([Gtk.AccessibleProperty.LABEL], [text("recording.title.label")])
        self.title_entry.connect("changed", self._title_changed)
        title_box.append(self.title_entry)
        title_box.append(label(text("recording.title.caption"), "small", "dim-label", xalign=0))
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
        self.mic_level = Gtk.ProgressBar(width_request=96, valign=Gtk.Align.CENTER)
        self.mic_row.append(self.mic_level)
        self.check = Gtk.Button(label=text("errors.micSilentRow.action"), valign=Gtk.Align.CENTER, visible=False)
        self.check.connect("clicked", self._check_sound)
        self.mic_row.append(self.check)
        sources.append(self.mic_row)
        sources.append(Gtk.Separator())
        others = Gtk.Box(spacing=14, css_classes=["recording-source"])
        others.append(Gtk.Image(icon_name="computer-symbolic"))
        others_labels = vertical(2, hexpand=True)
        others_labels.append(label(text("common.theOthers"), "heading", xalign=0))
        self.others_caption = label("", "small", "dim-label", xalign=0)
        others_labels.append(self.others_caption)
        others.append(others_labels)
        self.others_level = Gtk.ProgressBar(width_request=96, valign=Gtk.Align.CENTER)
        others.append(self.others_level)
        others.append(Gtk.Box(width_request=66))
        sources.append(others)
        sources.append(Gtk.Separator())
        sources.append(
            Gtk.Button(
                label=text("recording.changeWhatIsRecorded"),
                sensitive=False,
                css_classes=["flat", "recording-change"],
                halign=Gtk.Align.START,
            )
        )
        column.append(sources)
        note_box = vertical(6)
        note_box.append(label(text("recording.notes.label"), "small", "heading", "dim-label", xalign=0))
        self.notes_view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            height_request=88,
            top_margin=10,
            bottom_margin=10,
            left_margin=12,
            right_margin=12,
            css_classes=["recording-notes"],
        )
        self.notes_view.update_property([Gtk.AccessibleProperty.LABEL], [text("recording.notes.label")])
        self.notes = TimedNotes(
            self.notes_view.get_buffer(), meeting.metadata.get("notes", []), self.elapsed, self._changed
        )
        note_box.append(self.notes_view)
        note_box.append(label(text("recording.notes.caption"), "small", "dim-label", xalign=0))
        column.append(note_box)
        self.update_status()

    def elapsed(self):
        return self.app.comfort.elapsed(self.meeting)

    def _check_sound(self, *_):
        # The desktop owns its sound preferences. No change to devices by Cassini.
        Gio.AppInfo.create_from_commandline("gnome-control-center sound", None, Gio.AppInfoCreateFlags.NONE).launch(
            [], None
        )

    def _title_changed(self, *_):
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
        future = self.window.monitor._executor.submit(
            edit_recording, self.meeting.directory, title=self.title_entry.get_text(), notes=self.notes.values()
        )
        future.add_done_callback(lambda f: GLib.idle_add(self._saved, f))
        return False

    def _saved(self, future):
        try:
            future.result()
        except (OSError, ValueError) as error:
            self.app.recording_error = str(error)  # Keep existing behavior until error copy is published.
        if not self.window.monitor._closed:
            self.window.monitor.refresh({self.meeting.directory})
        return False

    def update(self, meeting):
        self.meeting = meeting
        if not self._dirty and not self.title_entry.has_focus():
            self.headline.set_label(meeting.title)
        self.update_status()

    def update_status(self):
        status = self.app.comfort.status(self.meeting)
        self.timer.set_label(fmt.live_big(self.elapsed()))
        self.stop.set_sensitive(self.meeting.lifecycle == "recording")
        silent = status["silent"]
        self.mic_warning.set_visible(silent)
        self.check.set_visible(silent)
        (self.mic_row.add_css_class if silent else self.mic_row.remove_css_class)("mic-silent")
        for bar, level in zip((self.mic_level, self.others_level), status["levels"], strict=True):
            bar.set_fraction(level)
        browser = self.meeting.capture_source != "gnome"
        self.others_caption.set_label(
            text("recording.others.browserVoices", n=status["voices"]) if browser else text("recordFirst.sound.title")
        )
        signature = status["line"], status["minutes"]
        if signature != self._line_signature:
            self._line_signature = signature
            while child := self.lines.get_first_child():
                self.lines.remove(child)
            self.lines.set_visible(bool(signature[0]))
            if signature[0]:
                self.lines.append(InlineLine(signature[0], n=signature[1]))

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        self._save()
        if self.capture._controls in self.window._capture_headers:
            self.window._capture_headers.remove(self.capture._controls)
