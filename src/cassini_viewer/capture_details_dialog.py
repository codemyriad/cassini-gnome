# SPDX-License-Identifier: GPL-3.0-or-later
"""D28 Part A design draft. Provisional English; no capture actions or writes."""

import threading
import weakref

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .capture_details import UNKNOWN, read_details


def column(spacing=6, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


def label(value, *classes, **kwargs):
    return Gtk.Label(
        label=value, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=list(classes), **kwargs
    )


class RecordingDetailsDialog(Adw.Dialog):
    def __init__(self, window, meeting):
        super().__init__(title="Recording details", content_width=900, content_height=620)
        self.window, self.meeting = window, meeting
        self.external = window._external_meeting
        self.closed = self.busy = False
        self.report = None
        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)
        header = Adw.HeaderBar()
        self.heading = Adw.WindowTitle(title="Recording details", subtitle=meeting.title)
        header.set_title_widget(self.heading)
        toolbar.add_top_bar(header)
        self.body = column(18, margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        self.body.append(label("Reading recording details…", "dim-label"))
        toolbar.set_content(Gtk.ScrolledWindow(child=self.body, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        footer = column(8, margin_start=24, margin_end=24, margin_top=12, margin_bottom=18)
        footer.append(label("Files", "heading"))
        row = Gtk.Box(spacing=10)
        self.folder = Gtk.Label(xalign=0, selectable=True, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.folder.add_css_class("dim-label")
        self.folder.set_max_width_chars(45)
        row.append(self.folder)
        self.open_button = Gtk.Button(label="Open folder", sensitive=False)
        self.open_button.connect("clicked", self._open)
        row.append(self.open_button)
        self.copy_button = Gtk.Button(label="Copy details", sensitive=False, css_classes=["suggested-action"])
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
                result, error = None, "Couldn’t read recording details. Close this dialog and try again."
            dialog = reference()
            if dialog is not None and not dialog.closed:
                GLib.idle_add(dialog._loaded, meeting.directory, result, error)

        # No application hold, GTK IO, subprocess or non-daemon worker that can
        # keep Quit alive. At most one bounded read is outstanding per dialog.
        threading.Thread(target=load, name="cassini-recording-details", daemon=True).start()
        return True

    def _loaded(self, directory, report, error):
        self.busy = False
        if self.closed:
            return False
        if directory != self._current().directory:
            self.refresh()
            return False
        if report == self.report and error is None:
            return False
        first = self.report is None
        self.report = report
        while self.body.get_first_child():
            self.body.remove(self.body.get_first_child())
        self.copy_button.set_sensitive(report is not None)
        self.open_button.set_sensitive(report is not None)
        if error:
            self.body.append(label(error, "dim-label"))
            return False
        self.heading.set_subtitle(report.title + (" · Live" if report.live else ""))
        self.folder.set_label(report.folder)
        self.folder.set_tooltip_text(report.folder)
        captured = column(10)
        caption = Gtk.Box(spacing=12)
        caption.append(label("How it was captured", "title-3", hexpand=True))
        if report.live:
            caption.append(label("Updates while recording", "small", "dim-label"))
        captured.append(caption)
        sides = Gtk.Box(spacing=14, homogeneous=True)
        for side in ("Me", "The others"):
            panel = column(4, css_classes=["details-side"], valign=Gtk.Align.START)
            panel.append(label(side, "heading"))
            spans = [span for span in report.spans if span.side == side]
            if not spans:
                panel.append(label("No source details recorded", "dim-label"))
            for span in spans[:20]:
                row = Gtk.Box(spacing=10, css_classes=["details-span"])
                row.append(Gtk.Box(width_request=3, css_classes=["details-marker"], margin_top=4, margin_bottom=4))
                content = column(2, hexpand=True)
                title = Gtk.Box(spacing=8)
                title.append(label(span.source, "heading", hexpand=True))
                title.append(label(span.time, "small", "details-time", valign=Gtk.Align.START))
                content.append(title)
                content.append(label(span.method, "small", "dim-label"))
                if span.reason:
                    content.append(label(span.reason, "small", "dim-label"))
                row.append(content)
                panel.append(row)
            if len(spans) > 20:
                panel.append(label("More spans are included in Copy details.", "small", "dim-label"))
            sides.append(panel)
        captured.append(sides)
        self.body.append(captured)
        for note in report.notes:
            self.body.append(label(note, "small", "dim-label"))
        facts = Gtk.Grid(column_spacing=24, column_homogeneous=True)
        left = column(14)
        ended = column(4)
        ended.append(label("How it ended", "heading"))
        ended.append(label(report.ended, "dim-label" if report.ended == UNKNOWN else "body"))
        left.append(ended)
        left.append(self._facts("Audio health", report.health))
        facts.attach(left, 0, 0, 1, 1)
        facts.attach(self._facts("Writing down", report.writing), 1, 0, 1, 1)
        self.body.append(facts)
        if first:
            self.copy_button.grab_focus()
        return False

    @staticmethod
    def _facts(title, rows):
        content = column(8, valign=Gtk.Align.START)
        content.append(label(title, "heading"))
        grid = Gtk.Grid(column_spacing=12, row_spacing=7)
        for index, (name, value) in enumerate(rows):
            key = label(name, "small", "dim-label", valign=Gtk.Align.START, max_width_chars=18)
            key.set_size_request(110, -1)
            grid.attach(key, 0, index, 1, 1)
            value_label = label(value, "small", hexpand=True, selectable=True, max_width_chars=35)
            if value == UNKNOWN:
                value_label.add_css_class("dim-label")
            grid.attach(value_label, 1, index, 1, 1)
        content.append(grid)
        return content

    def _copy(self, *_):
        if self.report:
            self.get_display().get_clipboard().set(self.report.plain_text())
            self.window.toast("Recording details copied")

    def _open(self, *_):
        if self.report:
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self.report.folder))
            launcher.launch(self.window, None, self._opened)

    def _opened(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error:
            if not self.closed:
                self.window.toast("Couldn’t open the recording folder")
