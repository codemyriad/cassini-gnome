# SPDX-License-Identifier: GPL-3.0-or-later
"""D28 Part A: recording details, with bounded reads and stable live widgets."""

import threading
import weakref

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import ux_formats as fmt
from .capture_details import UNKNOWN, read_details, side_title
from .strings import text


def column(spacing=6, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def label(value, *classes, **kwargs):
    return Gtk.Label(
        label=value, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=list(classes), **kwargs
    )


WIDE_PX = 760  # GnomeMocks-RecordingDetails: the wide dialog.


class _Pair(Gtk.Grid):
    """Equal columns, filled left to right by append()."""

    def __init__(self):
        super().__init__(column_spacing=14, column_homogeneous=True)
        self._next = 0

    def append(self, child):
        self.attach(child, self._next, 0, 1, 1)
        self._next += 1


class RecordingDetailsDialog(Adw.Dialog):
    def __init__(self, window, meeting):
        super().__init__(title=text("details.title"), follows_content_size=True)
        self.window, self.meeting = window, meeting
        self.external = window._external_meeting
        self.closed = self.busy = False
        self.report = None
        self._last_error = None
        self._shape = None
        self._bindings = {}
        self.narrow = False
        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)
        header = Adw.HeaderBar()
        self.heading = Adw.WindowTitle(title=text("details.title"), subtitle=meeting.title)
        header.set_title_widget(self.heading)
        toolbar.add_top_bar(header)
        self.body = column(18, margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        self.body.append(label(text("details.reading"), "dim-label"))
        self.scroller = Gtk.ScrolledWindow(
            child=self.body, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True
        )
        toolbar.set_content(self.scroller)
        footer = column(8, margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        footer.append(label(text("details.files.title"), "heading"))
        row = self.files_row = Gtk.Box(spacing=10)
        self.folder = Gtk.Label(xalign=0, selectable=True, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.folder.add_css_class("dim-label")
        self.folder.set_max_width_chars(45)
        row.append(self.folder)
        # D-1162: narrow, Open folder and Copy details share one line under the path.
        self.files_buttons = Gtk.Box(spacing=10)
        self.open_button = Gtk.Button(label=text("details.openFolder"), sensitive=False)
        self.open_button.connect("clicked", self._open)
        self.files_buttons.append(self.open_button)
        self.copy_button = Gtk.Button(label=text("details.copy"), sensitive=False, css_classes=["suggested-action"])
        self.copy_button.connect("clicked", self._copy)
        self.set_default_widget(self.copy_button)
        self.files_buttons.append(self.copy_button)
        row.append(self.files_buttons)
        footer.append(row)
        toolbar.add_bottom_bar(footer)
        # D-1162: the dialog follows its content, so the window decides the width class: wide, the
        # cards sit side by side in about 760 px; narrow (a phone-width window), they stack.
        self._set_narrow(0 < window.get_width() < WIDE_PX + 48)
        if not self.narrow:
            self.scroller.set_min_content_width(WIDE_PX - 48)
        self.connect("closed", self._closed)
        self.timer = GLib.timeout_add_seconds(1, self.refresh)
        self.refresh()

    def _set_narrow(self, narrow):
        """D-1162: the narrow dialog stacks the same cards; Files puts its two buttons under the path."""
        if narrow == self.narrow:
            return
        self.narrow = narrow
        self.files_row.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self.files_buttons.set_homogeneous(narrow)
        if self.report is not None:
            # The shape includes the width class, so the next load rebuilds the cards; do it now.
            report, self.report = self.report, None
            self._loaded(self._current().directory, report, self._last_error)

    def _closed(self, *_):
        self.closed = True
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _current(self):
        if not self.external and self.window.snapshot:
            self.meeting = next((m for m in self.window.snapshot.meetings if m.key == self.meeting.key), self.meeting)
        return self.meeting

    def refresh(self):
        if self.closed:
            return False
        if self.busy:
            return True
        self.busy = True
        meeting = self._current()
        reference = weakref.ref(self)

        def load():
            try:
                result = read_details(meeting)
                error = None
            except Exception:
                result, error = None, text("details.readFailed")
            dialog = reference()
            if dialog is not None and not dialog.closed:
                GLib.idle_add(dialog._loaded, meeting.directory, result, error)

        # No application hold, GTK IO, subprocess or non-daemon worker that can
        # keep Quit alive. At most one bounded read is outstanding per dialog.
        threading.Thread(target=load, name="cassini-recording-details", daemon=True).start()
        return True

    @staticmethod
    def _span_line(span):
        """D-1162: "Built-in Audio Analog Stereo · 0:00 to 52:00", in plain words."""
        parts = []
        if span.method and span.method != text("details.method.microphone"):
            parts.append(span.method)  # "Microphone input" only repeats the source's name.
        if span.start_ms is None or span.end_ms is None:
            parts.append(text("details.timingUnknown"))
        else:
            parts.append(
                text("details.span.range", start=fmt.live_badge(span.start_ms), end=fmt.live_badge(span.end_ms))
            )
        if span.reason:
            parts.append(span.reason)
        return " · ".join(parts)

    @staticmethod
    def _quiet_lines(report):
        """Warnings that still matter once Audio sources folds into the cards (D-1162): a source's
        problem, its gaps, the computer being busy, and the notes, except the one that only says
        what was not recorded."""
        lines = []
        for source in report.audio:
            if source.warning:
                lines.append((source.text, "warning"))
            lines.extend((line, "warning") for line in source.gap_lines)
        lines.extend((line, "dim-label") for line in report.computer_busy)
        lines.extend((note, "dim-label") for note in report.notes if note != text("details.timelineUnknown"))
        return lines

    def _presentation(self, report):
        values = {}
        counts = []
        for side in ("Me", "The others"):
            spans = [span for span in report.spans if span.side == side]
            counts.append((min(20, len(spans)), len(spans) > 20))
            for index, span in enumerate(spans[:20]):
                values[("span", side, index, "source")] = span.source
                values[("span", side, index, "line")] = self._span_line(span)
        silent = []
        for side in ("Me", "The others"):
            lines = report.silent_lines(side)
            silent.append(len(lines))
            for index, line in enumerate(lines):
                values[("silent", side, index)] = line
        if report.ended_plain is not None:
            values[("ended-label",)], values[("ended",)] = report.ended_plain
        health = tuple((name, value) for name, value in report.health if value != UNKNOWN)
        writing = report.writing_plain if not report.live else ()
        for group, rows in (("health", health), ("writing", writing)):
            for index, (_name, value) in enumerate(rows):
                values[(group, index)] = value
        quiet = self._quiet_lines(report)
        for index, (line, _style) in enumerate(quiet):
            values[("quiet", index)] = line
        shape = (
            self.narrow,
            tuple(counts),
            tuple(silent),
            report.ended_plain is not None and bool(report.ended_plain[0]),
            report.ended_plain is not None,
            tuple(k for k, _ in health),
            tuple(k for k, _ in writing),
            tuple(style for _line, style in quiet),
        )
        return shape, values, health, writing, quiet

    def _bound(self, key, value, *classes, **kwargs):
        widget = label(value, *classes, **kwargs)
        self._bindings[key] = widget
        return widget

    def _loaded(self, directory, report, error):
        self.busy = False
        if self.closed:
            return False
        if directory != self._current().directory:
            self.refresh()
            return False
        if report == self.report and error == self._last_error:
            return False
        first = self.report is None
        self.report, self._last_error = report, error
        self.copy_button.set_sensitive(report is not None)
        self.open_button.set_sensitive(report is not None)
        if error:
            self._shape = None
            self._bindings.clear()
            while self.body.get_first_child():
                self.body.remove(self.body.get_first_child())
            self.body.append(label(error, "dim-label"))
            return False
        shape, values, health, writing, quiet = self._presentation(report)
        if shape != self._shape:
            self._shape = shape
            self._bindings.clear()
            self._build_body(report, health, writing, quiet)
        # Normal recording ticks only change values. Keep the widgets, focus,
        # selection and scroll position instead of rebuilding the whole body.
        for key, widget in self._bindings.items():
            value = values[key]
            if widget.get_label() != value:
                widget.set_label(value)
            if key[0] == "ended-label":
                widget.set_visible(bool(value))
        subtitle = text("details.liveSubtitle", title=report.title) if report.live else report.title
        if self.heading.get_subtitle() != subtitle:
            self.heading.set_subtitle(subtitle)
        if self.folder.get_label() != report.folder:
            self.folder.set_label(report.folder)
            self.folder.set_tooltip_text(report.folder)
        self.live_hint.set_visible(report.live)
        if first:
            self.copy_button.grab_focus()
        return False

    def _build_body(self, report, health, writing, quiet):
        """D-1162 (L-82): every section is its own card, each detail a small grey label above its value.
        Wide puts Me and The others side by side, then How it ended and Audio health; narrow stacks the
        same cards. Rows with nothing recorded are left out; Audio sources folds into the side cards."""
        while self.body.get_first_child():
            self.body.remove(self.body.get_first_child())
        captured = column(10)
        caption = Gtk.Box(spacing=12)
        caption.append(label(text("details.capture.title"), "title-3", hexpand=True))
        self.live_hint = label(text("details.updates"), "small", "dim-label")
        caption.append(self.live_hint)
        captured.append(caption)
        sides = self._pair()
        for side in ("Me", "The others"):
            card = column(8, css_classes=["card", "details-card", "details-side"], valign=Gtk.Align.START)
            card.append(label(side_title(side), "heading"))
            spans = [span for span in report.spans if span.side == side]
            if not spans:
                # L-84 (open): the plainer line for The others is proposed in the PR for Alex.
                empty = "details.capture.othersNothing" if side == "The others" else "details.capture.empty"
                card.append(label(text(empty), "dim-label"))
            for index, span in enumerate(spans[:20]):
                row = column(2, css_classes=["details-span"])
                row.append(self._bound(("span", side, index, "source"), span.source, "details-span-source"))
                row.append(
                    self._bound(("span", side, index, "line"), self._span_line(span), "dim-label", "details-span-line")
                )
                card.append(row)
            if len(spans) > 20:
                card.append(label(text("details.capture.more"), "small", "dim-label"))
            # R13: a captured source that heard no speech keeps its neutral line in its own card.
            for index, line in enumerate(report.silent_lines(side)):
                card.append(self._bound(("silent", side, index), line, "small", "dim-label"))
            sides.append(card)
        captured.append(sides)
        for index, (line, style) in enumerate(quiet):
            captured.append(self._bound(("quiet", index), line, "small", style))
        self.body.append(captured)
        sections = []
        if report.ended_plain is not None:
            rows = [(("ended-label",), ("ended",))]
            sections.append((self._section(text("details.end.title"), rows, columns=1), False))
        if health:
            rows = [((None, name), ("health", index)) for index, (name, _value) in enumerate(health)]
            sections.append((self._section(text("details.health.title"), rows, columns=1), False))
        if writing:
            rows = [((None, name), ("writing", index)) for index, (name, _value) in enumerate(writing)]
            # With several details, Transcribing is one two-column card across the dialog (wide).
            full = not self.narrow and len(rows) > 1
            sections.append((self._section(text("details.writing.title"), rows, columns=2 if full else 1), full))
        # Wide: two cards side by side, so two sections can't read as one block; narrow stacks them.
        pair, paired = None, 0
        for section, full in sections:
            if full or self.narrow:
                self.body.append(section)
                pair = None
                continue
            if pair is None or paired == 2:
                pair, paired = self._pair(), 0
                self.body.append(pair)
            pair.append(section)
            paired += 1

    def _pair(self):
        """Two cards side by side on a wide window, stacked on a narrow one.

        Side by side, these height-for-width cards are negotiated by a grid: a horizontal box asks
        them for their width at the constrained viewport height."""
        if self.narrow:
            return column(14)
        return _Pair()

    def _section(self, title, rows, *, columns):
        """A heading above one card of property rows (libadwaita's small grey label above its value)."""
        section = column(8, valign=Gtk.Align.START)
        section.append(label(title, "heading"))
        card = Gtk.Grid(column_homogeneous=True, css_classes=["card", "details-card", "details-properties"])
        for index, (name, value_key) in enumerate(rows):
            cell = column(2, css_classes=["details-property"])
            if name[0] is None:
                cell.append(label(name[1], "caption", "dim-label"))
            else:
                cell.append(self._bound(name, "", "caption", "dim-label"))
            cell.append(self._bound(value_key, "", "details-value", selectable=True))
            card.attach(cell, index % columns, index // columns, 1, 1)
        section.append(card)
        return section

    def _copy(self, *_):
        if self.report:
            self.get_display().get_clipboard().set(self.report.plain_text())
            self.window.toast(text("details.copied"))

    def _open(self, *_):
        if self.report:
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self.report.folder))
            launcher.launch(self.window, None, self._opened)

    def _opened(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error:
            if not self.closed:
                self.window.toast(text("details.openFailed"))
