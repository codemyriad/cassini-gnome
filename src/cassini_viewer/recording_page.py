# SPDX-License-Identifier: GPL-3.0-or-later
"""UX6: pushed live recording page; app-owned titles and time-pinned notes."""

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from . import ux_formats as fmt
from .inline_line import InlineLine
from .library_window import label, vertical
from .strings import text


class TimedNotes:
    """Edit note bodies; timestamps are display spans, never editable content."""

    def __init__(self, buffer, notes, elapsed, changed):
        self.buffer, self.elapsed, self.changed = buffer, elapsed, changed
        self.guard = False
        self.lines = [(n["atMs"], n["text"]) for n in notes] or [(None, "")]
        self.history = []
        self.history_position = -1
        buffer.begin_irreversible_action()
        self._render(0, 0)
        buffer.end_irreversible_action()
        for action in ("undo", "redo"):
            buffer.connect(action, self._history_begin)
            buffer.connect_after(action, self._history_end, action)
        buffer.connect("insert-text", self._insert)
        buffer.connect("delete-range", self._delete)

    @staticmethod
    def _prefix(at):
        return fmt.position(at) + "  " if at is not None else ""

    def _point(self, offset):
        base = 0
        for index, (at, body) in enumerate(self.lines):
            prefix = len(self._prefix(at))
            end = base + prefix + len(body)
            if offset <= end or index == len(self.lines) - 1:
                return index, min(len(body), max(0, offset - base - prefix))
            base = end + 1
        raise AssertionError("Missing note line")

    @staticmethod
    def _text(lines):
        return "\n".join(TimedNotes._prefix(at) + body for at, body in lines)

    def _history_begin(self, _buffer):
        # GTK replays rendered text, including timestamp prefixes. Let that
        # replay finish without treating its inserts as newly typed notes.
        self.guard = True

    def _history_end(self, buffer, action):
        value = buffer.get_text(*buffer.get_bounds(), True)
        positions = (
            range(self.history_position - 1, -1, -1)
            if action == "undo"
            else range(self.history_position + 1, len(self.history))
        )
        for position in positions:
            if self._text(self.history[position]) == value:
                self.history_position = position
                self.lines = list(self.history[position])
                break
        else:
            # An undo with no available action leaves the current text intact.
            assert self._text(self.lines) == value, "Unknown note undo state"
        self.guard = False
        self.changed()

    def _render(self, line, column):
        value = self._text(self.lines)
        if self.history_position < 0 or self.history[self.history_position] != self.lines:
            del self.history[self.history_position + 1 :]
            self.history.append(list(self.lines))
            self.history_position = len(self.history) - 1
        old = self.buffer.get_text(*self.buffer.get_bounds(), True)
        # A minimal edit keeps Gtk's native user-action/undo grouping and avoids
        # replacing every note when one character changes.
        left = 0
        while left < min(len(old), len(value)) and old[left] == value[left]:
            left += 1
        right = 0
        while right < min(len(old), len(value)) - left and old[-right - 1] == value[-right - 1]:
            right += 1
        self.guard = True
        self.buffer.begin_user_action()
        try:
            self.buffer.delete(self.buffer.get_iter_at_offset(left), self.buffer.get_iter_at_offset(len(old) - right))
            self.buffer.insert(
                self.buffer.get_iter_at_offset(left), value[left : len(value) - right if right else None]
            )
            offset = sum(len(self._prefix(at)) + len(body) + 1 for at, body in self.lines[:line])
            offset += len(self._prefix(self.lines[line][0])) + column
            self.buffer.place_cursor(self.buffer.get_iter_at_offset(offset))
        finally:
            self.buffer.end_user_action()
            self.guard = False

    def _insert(self, buffer, iterator, value, _length):
        if self.guard:
            return
        buffer.stop_emission_by_name("insert-text")
        line, column = self._point(iterator.get_offset())
        at, body = self.lines[line]
        parts = value.split("\n")
        now = int(self.elapsed())
        replacement = [(at if at is not None else now, body[:column] + parts[0])]
        replacement.extend((now, part) for part in parts[1:])
        replacement[-1] = (replacement[-1][0], replacement[-1][1] + body[column:])
        if len(parts) > 1 and column == 0 and body:
            # Inserting a blank line before an existing note keeps that note's
            # pinned time; only newly typed content gets the current time.
            replacement[0] = (now, replacement[0][1])
            replacement[-1] = (at, replacement[-1][1])
        self.lines[line : line + 1] = replacement
        self._render(line + len(parts) - 1, (column if len(parts) == 1 else 0) + len(parts[-1]))
        self.changed()

    def _delete(self, buffer, start, end):
        if self.guard:
            return
        buffer.stop_emission_by_name("delete-range")
        first, left = self._point(start.get_offset())
        last, right = self._point(end.get_offset())
        if (first, left) == (last, right):
            if first > 0 and left == 0 and end.get_offset() - start.get_offset() == 1:
                # Backspace from the body start crosses the protected prefix.
                # Merge with the previous body as a normal editor would.
                first -= 1
                left = len(self.lines[first][1])
            else:
                return  # Deleting only a timestamp span never deletes the note.
        at, body = self.lines[first]
        self.lines[first : last + 1] = [(at, body[:left] + self.lines[last][1][right:])]
        if len(self.lines) == 1 and not self.lines[0][1]:
            self.lines = [(None, "")]
        self._render(first, left)
        self.changed()

    def values(self):
        return [dict(atMs=int(at), text=body) for at, body in self.lines if at is not None and body.strip()]

    def reset(self, notes):
        self.buffer.begin_irreversible_action()
        self.history = []
        self.history_position = -1
        self.lines = [(n["atMs"], n["text"]) for n in notes] or [(None, "")]
        self._render(0, 0)
        self.buffer.end_irreversible_action()


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
        self._notes_dirty = False
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
        self.title_entry = Gtk.Entry(
            text=draft.get("title") or meeting.title, max_length=500, css_classes=["recording-title"]
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
        sources.append(self.change_sources)
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
        window.protect_text_input(self.notes_view)
        self.notes_view.update_property([Gtk.AccessibleProperty.LABEL], [text("recording.notes.label")])
        self.notes = TimedNotes(
            self.notes_view.get_buffer(),
            draft.get("notes", meeting.metadata.get("notes", [])),
            self.elapsed,
            self._notes_changed,
        )
        note_box.append(self.notes_view)
        note_box.append(label(text("recording.notes.caption"), "small", "dim-label", xalign=0))
        column.append(note_box)
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

    def _notes_changed(self):
        self._notes_dirty = True
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
        if self._notes_dirty:
            changes["notes"] = self.notes.values()
        self._title_dirty = self._notes_dirty = False
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
            self._notes_dirty |= "notes" in changes
            self._dirty = True
        if not self.window.monitor._closed:
            self.window.monitor.refresh({self.meeting.directory})
        return False

    def update(self, meeting):
        self.meeting = meeting
        if self.sources_dialog is not None:
            self.sources_dialog.update(meeting)
        draft = self.app.comfort.draft(meeting)
        if not self._dirty and not self._saving and not self.title_entry.has_focus():
            self._updating_title = True
            self.title_entry.set_text(draft.get("title") or meeting.title)
            self._updating_title = False
            self.headline.set_label(self.title_entry.get_text())
        if not self._notes_dirty and not self._saving and not self.notes_view.has_focus():
            notes = draft.get("notes", meeting.metadata.get("notes", []))
            if self.notes.values() != notes:
                self.notes.reset(notes)
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

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.sources_dialog is not None and not self.sources_dialog.closed:
            self.sources_dialog.force_close()
        self.window._editor_focus(self.title_entry, False)
        self.window._editor_focus(self.notes_view, False)
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        self._save()
        if self.capture._controls in self.window._capture_headers:
            self.window._capture_headers.remove(self.capture._controls)
