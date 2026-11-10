# SPDX-License-Identifier: GPL-3.0-or-later
"""Spike: exact prefix geometry with native GtkScrolledWindow input.

Only visible rows are parented. All row heights are measured before publishing
an adjustment range. GtkLabel/Pango's cached height-for-width is authoritative;
realizing a row never changes the range. No gesture or kinetic controller here.
"""

import time
from bisect import bisect_right

from gi.repository import GObject, Graphene, Gsk, Gtk


class ExactTranscript(Gtk.Widget, Gtk.Scrollable):
    __gtype_name__ = "CassiniExactTranscript"
    hadjustment = GObject.Property(type=Gtk.Adjustment)
    vadjustment = GObject.Property(type=Gtk.Adjustment)
    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)

    def __init__(self, page):
        super().__init__(css_classes=["meeting-transcript"], hexpand=True, vexpand=True)
        self.page = page
        self.items = ()
        self.prefix = [0]
        self._prefix_by_width = {}
        self.rows = {}
        self.layout_width = -1
        self.ready = False
        self._adjustment_handler = None
        self._pending_scroll = None
        self.connect("notify::vadjustment", self._adjustment_changed)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.connect("notify::scale-factor", self.invalidate)
        settings = self.get_settings()
        self._font_signals = [
            (settings, settings.connect("notify::" + name, self.invalidate))
            for name in ("gtk-font-name", "gtk-xft-dpi")
        ]

    def _adjustment_changed(self, *_):
        if self._adjustment_handler:
            obj, handler = self._adjustment_handler
            obj.disconnect(handler)
        adjustment = self.props.vadjustment
        if adjustment:
            handler = adjustment.connect("value-changed", lambda *_: self.queue_allocate())
            self._adjustment_handler = adjustment, handler

    def set_turns(self, turns):
        self.clear_rows()
        self.items = turns
        self.invalidate()

    def invalidate(self, *_args):
        self._prefix_by_width.clear()
        self.layout_width = -1
        self.ready = False
        self.queue_allocate()

    def clear_rows(self):
        for index, row in list(self.rows.items()):
            if self.page.bound.get(index, (None, None, None))[2] is row:
                self.page.bound.pop(index, None)
            row.unparent()
        self.rows.clear()

    def close(self):
        self.clear_rows()
        for obj, handler in self._font_signals:
            obj.disconnect(handler)
        self._font_signals.clear()
        if self._adjustment_handler:
            obj, handler = self._adjustment_handler
            obj.disconnect(handler)
            self._adjustment_handler = None

    def do_measure(self, orientation, for_size):
        # The scrollable requests only a viewport, never the full content size.
        return (0, 360, -1, -1) if orientation == Gtk.Orientation.HORIZONTAL else (0, 100, -1, -1)

    def _measure_all(self, width):
        self.clear_rows()
        if width in self._prefix_by_width:
            self.prefix = self._prefix_by_width[width]
            self.layout_width = width
            self.ready = True
            self.layout_completed_cpu_ns = time.thread_time_ns()
            return
        prefix = [0]
        # A single probe has the same CSS ancestry as live rows. Reuse its
        # labels so full indexing does not construct one widget tree per turn.
        # GtkLabel's native measurement uses the bounded Pango geometry cache.
        if self.items:
            from . import ux_formats as fmt

            probe = self.page._build_row(self.items[0], register=False)
            probe.set_parent(self)
            headers = probe.get_first_child()
            words = probe.get_last_child()
            for turn in self.items:
                words._reading_signature = None
                self.page._render_words(turn, words)
                self.page._refresh_header(turn, probe)
                headers.get_last_child().set_label(fmt.position(turn.start_ms))
                height = probe.measure(Gtk.Orientation.VERTICAL, width).natural
                prefix.append(prefix[-1] + height)
            probe.unparent()
        if len(self._prefix_by_width) >= 3:
            del self._prefix_by_width[next(iter(self._prefix_by_width))]
        self._prefix_by_width[width] = prefix
        self.prefix = prefix
        self.layout_width = width
        self.ready = True
        self.layout_completed_cpu_ns = time.thread_time_ns()

    def do_size_allocate(self, width, height, baseline):
        # GtkWidget removes its CSS padding before calling this vfunc.
        if self.layout_width != width:
            old_index = max(0, bisect_right(self.prefix, self._value()) - 1)
            old_offset = self._value() - self.prefix[min(old_index, len(self.prefix) - 1)]
            self._measure_all(width)
            value = self.prefix[min(old_index, len(self.items))] + old_offset
        else:
            value = self._value()
        adjustment = self.props.vadjustment
        if adjustment:
            adjustment.configure(value, 0, max(height, self.prefix[-1]), 40, height * 0.9, height)
        if self.props.hadjustment:
            self.props.hadjustment.configure(0, 0, width, 40, width * 0.9, width)
        if self._pending_scroll is not None:
            index = self._pending_scroll
            self._pending_scroll = None
            self.scroll_to(index)
        value = self._value()
        first = max(0, bisect_right(self.prefix, max(0, value - height)) - 1)
        end = min(len(self.items), bisect_right(self.prefix, value + height * 2))
        needed = set(range(first, end))
        for index in self.rows.keys() - needed:
            row = self.rows.pop(index)
            self.page.bound.pop(index, None)
            row.unparent()
        for index in range(first, end):
            row = self.rows.get(index)
            if row is None:
                row = self.page._build_row(self.items[index])
                row.set_parent(self)
                self.rows[index] = row
            point = Graphene.Point().init(0, self.prefix[index] - value)
            transform = Gsk.Transform.new().translate(point)
            row.allocate(width, self.prefix[index + 1] - self.prefix[index], -1, transform)

    def _value(self):
        return self.props.vadjustment.get_value() if self.props.vadjustment else 0

    def scroll_to(self, index, *_args):
        if not self.ready:
            self._pending_scroll = index
            return
        if 0 <= index < len(self.items) and self.props.vadjustment:
            self.props.vadjustment.clamp_page(self.prefix[index], self.prefix[index + 1])

    def do_snapshot(self, snapshot):
        for row in self.rows.values():
            self.snapshot_child(row, snapshot)
