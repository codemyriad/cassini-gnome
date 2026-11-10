# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact prefix geometry, built in frame-budgeted chunks, with native scrolling."""

import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field

from gi.repository import GLib, GObject, Graphene, Gsk, Gtk

from . import ux_formats as fmt
from .meeting_text import reading_markup

# Includes the existing 120 px name column. CSS outer padding is separate.
COLUMN_CAP = 720
BUILD_BUDGET_NS = 4_000_000


@dataclass
class WidthIndex:
    width: int
    prefix: list = field(default_factory=lambda: [0])
    complete: bool = False


class ExactTranscript(Gtk.Widget, Gtk.Scrollable):
    __gtype_name__ = "CassiniExactTranscript"
    hadjustment = GObject.Property(type=Gtk.Adjustment)
    vadjustment = GObject.Property(type=Gtk.Adjustment)
    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)

    def __init__(self, page):
        super().__init__(
            css_classes=["meeting-transcript"], hexpand=True, vexpand=True, accessible_role=Gtk.AccessibleRole.LIST
        )
        from .transcript_accessible import WordActions
        from .transcript_navigation import TranscriptNavigation

        self.page = page
        self.navigation = TranscriptNavigation(self)
        self.word_actions = WordActions(self)
        self.items = ()
        self._display_items = ()
        self.prefix = [0]
        self.rows = {}
        self.layout_width = -1
        self.ready = False
        self.viewport_ready = False
        self.measured_rows = 0
        self.measurement_cpu_ns = 0
        self.build_slice_cpu_max_ns = 0
        self._indices = OrderedDict()
        self._metric_markup = {}
        self._requested_width = -1
        self._active = None
        self._building = None
        self._probe = None
        self._build_tick = 0
        self._adjustment_handler = None
        self._pending_scroll = None
        self.connect("notify::vadjustment", self._adjustment_changed)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.connect("notify::scale-factor", self.invalidate)
        settings = self.get_settings()
        self._font_signals = [
            (settings, settings.connect("notify::" + name, self.invalidate))
            for name in (
                "gtk-font-name",
                "gtk-xft-dpi",
                "gtk-xft-antialias",
                "gtk-xft-hinting",
                "gtk-xft-hintstyle",
                "gtk-xft-rgba",
                "gtk-theme-name",
                "gtk-fontconfig-timestamp",
            )
            if settings.find_property(name) is not None
        ]

    def _adjustment_changed(self, *_):
        if self._adjustment_handler:
            obj, handler = self._adjustment_handler
            obj.disconnect(handler)
        adjustment = self.props.vadjustment
        if adjustment:
            handler = adjustment.connect("value-changed", lambda *_: self.queue_allocate())
            self._adjustment_handler = adjustment, handler

    def set_turns(self, turns, common=0):
        # Build the changed suffix beside the old exact index. In particular,
        # replacing the sounding tail cannot temporarily shrink the range and
        # clamp the old anchor before its replacement height is known.
        self._cancel_build()
        self.items = turns
        updated = OrderedDict()
        for width, index in self._indices.items():
            prefix = index.prefix[: common + 1]
            updated[width] = WidthIndex(width, prefix, len(prefix) == len(turns) + 1)
        self._indices = updated
        self._metric_markup = {i: value for i, value in self._metric_markup.items() if i < common}
        self.ready = False
        self._requested_width = -1
        self.queue_allocate()

    def _cancel_build(self):
        if self._build_tick:
            self.remove_tick_callback(self._build_tick)
            self._build_tick = 0
        if self._probe:
            self._probe.close()
            self._probe.unparent()
            self._probe = None
        self._building = None

    def invalidate(self, *_args):
        self.word_actions.generation += 1
        self._cancel_build()
        self._indices.clear()
        self._metric_markup.clear()
        self._requested_width = -1
        self.ready = False
        self.queue_allocate()

    def clear_rows(self):
        self.word_actions.clear()
        for index, row in list(self.rows.items()):
            if self.page.bound.get(index, (None, None, None))[2] is row:
                self.page.bound.pop(index, None)
            row.close()
            row.unparent()
        self.rows.clear()

    def close(self):
        self._cancel_build()
        self.clear_rows()
        for obj, handler in self._font_signals:
            obj.disconnect(handler)
        self._font_signals.clear()
        if self._adjustment_handler:
            obj, handler = self._adjustment_handler
            obj.disconnect(handler)
            self._adjustment_handler = None

    def do_measure(self, orientation, for_size):
        return (0, 360, -1, -1) if orientation == Gtk.Orientation.HORIZONTAL else (0, 100, -1, -1)

    def _activate(self, index):
        old_row = max(0, bisect_right(self.prefix, self._value()) - 1)
        old_offset = self._value() - self.prefix[min(old_row, len(self.prefix) - 1)]
        focused = self.navigation.focused()
        for number, row in list(self.rows.items()):
            if number >= len(self.items) or row._turn != self.items[number]:
                removed = self.rows.pop(number)
                removed.close()
                removed.unparent()
                self.page.bound.pop(number, None)
                if focused == number:
                    self.navigation.pending = (min(number, len(self.items) - 1), None) if self.items else None
            else:
                row.update_relation([Gtk.AccessibleRelation.SET_SIZE], [len(self.items)])
        self._display_items = self.items
        self._active = index
        self.prefix = index.prefix
        self.layout_width = index.width
        if self.props.vadjustment and old_row + 1 < len(self.prefix):
            offset = min(old_offset, max(0, self.prefix[old_row + 1] - self.prefix[old_row] - 1))
            self._restore_value = self.prefix[old_row] + offset

    def _start_index(self, width):
        self._cancel_build()
        self._requested_width = width
        index = self._indices.get(width)
        if index is None:
            index = WidthIndex(width)
            self._indices[width] = index
        self._indices.move_to_end(width)
        while len(self._indices) > 3:
            self._indices.popitem(last=False)
        self.ready = index.complete
        if (self._active is None or index.complete) and self._active is not index:
            self._activate(index)
        if index.complete:
            return
        self._building = index
        if self.items:
            self._probe = self.page._build_row(self.items[0], register=False)
            self._probe.set_parent(self)
            self._probe.set_child_visible(False)
        if not self.items:
            self._advance_index()
        else:
            # Let the page chrome paint before measuring transcript text.
            self._build_tick = self.add_tick_callback(self._build_frame)

    def _advance_index(self):
        index = self._building
        if index is None:
            return
        started = time.thread_time_ns()
        while len(index.prefix) <= len(self.items):
            number = len(index.prefix) - 1
            turn = self.items[number]
            markup = self._metric_markup.get(number)
            if markup is None:
                # Only geometry: word highlight/future opacity never changes
                # metrics. Cache markup once instead of regenerating it on resize.
                markup = reading_markup(turn, -1, lambda speaker: self.page.meeting.speaker_for(speaker).name)
                self._metric_markup[number] = markup
            self._probe.get_last_child().set_markup(markup)
            self.page._refresh_header(turn, self._probe)
            self._probe.get_first_child().get_last_child().set_label(fmt.position(turn.start_ms))
            height = self._probe.measure(Gtk.Orientation.VERTICAL, index.width).natural
            index.prefix.append(index.prefix[-1] + height)
            self.measured_rows += 1
            # Publish the first covered viewport before measuring anything
            # below it. Subsequent frames resume the ordinary CPU slices.
            if (
                not self.viewport_ready
                and index is self._active
                and index.prefix[-1] >= self._value() + self._viewport_height
            ):
                break
            if time.thread_time_ns() - started >= BUILD_BUDGET_NS:
                break
        cost = time.thread_time_ns() - started
        self.measurement_cpu_ns += cost
        self.build_slice_cpu_max_ns = max(self.build_slice_cpu_max_ns, cost)
        if len(index.prefix) == len(self.items) + 1:
            index.complete = True
            self.ready = True
            self.layout_completed_cpu_ns = time.thread_time_ns()
            self._building = None
            if self._probe:
                self._probe.close()
                self._probe.unparent()
                self._probe = None

    def _build_frame(self, *_):
        self._advance_index()
        self.queue_allocate()
        if self._building is None:
            self._build_tick = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def do_size_allocate(self, width, height, baseline):
        if width <= 0 or height <= 0:
            return
        self._viewport_height = height
        target_width = min(width, COLUMN_CAP)
        if self._requested_width != target_width:
            self._start_index(target_width)
        target = self._indices[target_width]
        # Resize: retain old exact wrapping until a full replacement is ready.
        # Initial load: append only below the existing, immutable prefix.
        if target.complete and self._active is not target:
            self._activate(target)
        value = getattr(self, "_restore_value", self._value())
        if hasattr(self, "_restore_value"):
            del self._restore_value
        adjustment = self.props.vadjustment
        if adjustment:
            adjustment.configure(value, 0, max(height, self.prefix[-1]), 40, height * 0.9, height)
        if self.props.hadjustment:
            self.props.hadjustment.configure(0, 0, width, 40, width * 0.9, width)
        if self._pending_scroll is not None and self._pending_scroll + 1 < len(self.prefix):
            index = self._pending_scroll
            self._pending_scroll = None
            self.scroll_to(index)
        value = self._value()
        first = max(0, bisect_right(self.prefix, max(0, value - height)) - 1)
        end = min(len(self._display_items), len(self.prefix) - 1, bisect_right(self.prefix, value + height * 2))
        needed = set(range(first, end))
        focused = self.navigation.focused()
        if focused is not None:
            needed.add(focused)
        for index in self.rows.keys() - needed:
            row = self.rows.pop(index)
            self.page.bound.pop(index, None)
            row.close()
            row.unparent()
        for index in sorted(needed):
            row = self.rows.get(index)
            if row is None:
                row = self.page._build_row(self._display_items[index])
                following = next((self.rows[i] for i in sorted(self.rows) if i > index), None)
                row.insert_before(self, following)
                row.update_relation(
                    [Gtk.AccessibleRelation.POS_IN_SET, Gtk.AccessibleRelation.SET_SIZE], [index + 1, len(self.items)]
                )
                self.rows[index] = row
            point = Graphene.Point().init((width - self.layout_width) / 2, self.prefix[index] - value)
            transform = Gsk.Transform.new().translate(point)
            row.allocate(self.layout_width, self.prefix[index + 1] - self.prefix[index], -1, transform)
        self.word_actions.update(height)
        self.navigation.allocated()
        self.viewport_ready = bool(self.rows) and (self.prefix[-1] >= value + height or self.ready)

    def do_focus(self, direction):
        return self.navigation.focus(direction)

    def _value(self):
        return self.props.vadjustment.get_value() if self.props.vadjustment else 0

    def scroll_to(self, index, *_args):
        if not 0 <= index < len(self.items):
            return
        if index + 1 >= len(self.prefix):
            self._pending_scroll = index
            return
        if 0 <= index < len(self.items) and self.props.vadjustment:
            self.props.vadjustment.clamp_page(self.prefix[index], self.prefix[index + 1])

    def do_snapshot(self, snapshot):
        for row in self.rows.values():
            self.snapshot_child(row, snapshot)
