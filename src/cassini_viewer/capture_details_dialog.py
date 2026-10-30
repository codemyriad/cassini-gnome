# SPDX-License-Identifier: GPL-3.0-or-later
"""D28 Part A: recording details, with bounded reads and stable live widgets."""

import threading
import weakref

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .capture_details import UNKNOWN, read_details, side_title
from .strings import text


def column(spacing=6, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def label(value, *classes, **kwargs):
    return Gtk.Label(
        label=value, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=list(classes), **kwargs
    )


class RecordingDetailsDialog(Adw.Dialog):
    def __init__(self, window, meeting):
        super().__init__(title=text("details.title"), content_width=900, follows_content_size=True)
        self.window, self.meeting = window, meeting
        self.external = window._external_meeting
        self.closed = self.busy = False
        self.report = None
        self._last_error = None
        self._shape = None
        self._bindings = {}
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
        row = Gtk.Box(spacing=10)
        self.folder = Gtk.Label(xalign=0, selectable=True, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.folder.add_css_class("dim-label")
        self.folder.set_max_width_chars(45)
        row.append(self.folder)
        self.open_button = Gtk.Button(label=text("details.openFolder"), sensitive=False)
        self.open_button.connect("clicked", self._open)
        row.append(self.open_button)
        self.copy_button = Gtk.Button(label=text("details.copy"), sensitive=False, css_classes=["suggested-action"])
        self.copy_button.connect("clicked", self._copy)
        self.set_default_widget(self.copy_button)
        row.append(self.copy_button)
        footer.append(row)
        toolbar.add_bottom_bar(footer)
        self.connect("closed", self._closed)
        self.timer = GLib.timeout_add_seconds(1, self.refresh)
        self.refresh()

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
    def _presentation(report):
        values = {("ended",): report.ended}
        counts = []
        for side in ("Me", "The others"):
            spans = [span for span in report.spans if span.side == side]
            counts.append((min(20, len(spans)), len(spans) > 20))
            for index, span in enumerate(spans[:20]):
                for name in ("source", "time", "method", "reason"):
                    values[("span", side, index, name)] = getattr(span, name)
        for index, note in enumerate(report.notes):
            values[("note", index)] = note
        for group, rows in (("health", report.health), ("writing", report.writing)):
            for index, (_name, value) in enumerate(rows):
                values[(group, index)] = value
        shape = (
            tuple(counts),
            len(report.notes),
            tuple(k for k, _ in report.health),
            tuple(k for k, _ in report.writing),
        )
        return shape, values

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
        shape, values = self._presentation(report)
        if shape != self._shape:
            self._shape = shape
            self._bindings.clear()
            self._build_body(report)
        # Normal recording ticks only change values. Keep the widgets, focus,
        # selection and scroll position instead of rebuilding the whole body.
        for key, widget in self._bindings.items():
            value = values[key]
            if widget.get_label() != value:
                widget.set_label(value)
            if key[0] == "span" and key[-1] == "reason":
                widget.set_visible(bool(value))
            if key[0] in ("ended", "health", "writing"):
                (widget.add_css_class if value == UNKNOWN else widget.remove_css_class)("dim-label")
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

    def _build_body(self, report):
        while self.body.get_first_child():
            self.body.remove(self.body.get_first_child())
        captured = column(10)
        caption = Gtk.Box(spacing=12)
        caption.append(label(text("details.capture.title"), "title-3", hexpand=True))
        self.live_hint = label(text("details.updates"), "small", "dim-label")
        caption.append(self.live_hint)
        captured.append(caption)
        sides = Gtk.Box(spacing=14, homogeneous=True)
        for side in ("Me", "The others"):
            panel = column(4, css_classes=["details-side"], valign=Gtk.Align.START)
            panel.append(label(side_title(side), "heading"))
            spans = [span for span in report.spans if span.side == side]
            if not spans:
                panel.append(label(text("details.capture.empty"), "dim-label"))
            for index, span in enumerate(spans[:20]):
                row = Gtk.Box(spacing=10, css_classes=["details-span"])
                row.append(Gtk.Box(width_request=3, css_classes=["details-marker"], margin_top=4, margin_bottom=4))
                content = column(2, hexpand=True)
                title = Gtk.Box(spacing=8)
                title.append(self._bound(("span", side, index, "source"), span.source, "heading", hexpand=True))
                title.append(
                    self._bound(
                        ("span", side, index, "time"), span.time, "small", "details-time", valign=Gtk.Align.START
                    )
                )
                content.append(title)
                content.append(self._bound(("span", side, index, "method"), span.method, "small", "dim-label"))
                content.append(
                    self._bound(
                        ("span", side, index, "reason"), span.reason, "small", "dim-label", visible=bool(span.reason)
                    )
                )
                row.append(content)
                panel.append(row)
            if len(spans) > 20:
                panel.append(label(text("details.capture.more"), "small", "dim-label"))
            sides.append(panel)
        captured.append(sides)
        self.body.append(captured)
        for index, note in enumerate(report.notes):
            self.body.append(self._bound(("note", index), note, "small", "dim-label"))
        # These height-for-width columns must be negotiated by a grid. A
        # horizontal box asks them for width at the constrained viewport height.
        facts = Gtk.Grid(column_spacing=24, column_homogeneous=True)
        left = column(14)
        ended = column(4)
        ended.append(label(text("details.end.title"), "heading"))
        ended.append(self._bound(("ended",), report.ended, "body"))
        left.append(ended)
        left.append(self._facts(text("details.health.title"), report.health, "health"))
        facts.attach(left, 0, 0, 1, 1)
        facts.attach(self._facts(text("details.writing.title"), report.writing, "writing"), 1, 0, 1, 1)
        self.body.append(facts)

    def _facts(self, title, rows, group):
        content = column(8, valign=Gtk.Align.START)
        content.append(label(title, "heading"))
        grid = Gtk.Grid(column_spacing=12, row_spacing=7)
        for index, (name, value) in enumerate(rows):
            key = label(name, "small", "dim-label", valign=Gtk.Align.START, max_width_chars=18)
            key.set_size_request(110, -1)
            grid.attach(key, 0, index, 1, 1)
            value_label = self._bound((group, index), value, "small", hexpand=True, selectable=True, max_width_chars=35)
            grid.attach(value_label, 1, index, 1, 1)
        content.append(grid)
        return content

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
