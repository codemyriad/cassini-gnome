# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact prefix geometry, built in frame-budgeted chunks, with native scrolling."""

import time
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field

from gi.repository import GLib, GObject, Graphene, Gsk, Gtk

from .transcript_accessible import WordActions
from .transcript_geometry import DisplayPrefix
from .transcript_navigation import TranscriptNavigation

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

    def __init__(self, page, *, column_cap=COLUMN_CAP, css_class="meeting-transcript"):
        super().__init__(css_classes=[css_class], hexpand=True, vexpand=True, accessible_role=Gtk.AccessibleRole.LIST)

        self.page = page
        self.column_cap = column_cap
        self._closed = False
        self.navigation = TranscriptNavigation(self)
        self.word_actions = WordActions(self)
        self.items = ()
        self._display_items = ()
        self.prefix = [0]
        self.rows = {}
        self._retained_rows = set()
        self.visible_rows = range(0)
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
        self._pending_anchor = None
        self._display_common = None
        self._display_dirty = False
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
        if self._closed:
            return
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
        pending_scroll = self._pending_scroll
        self._pending_scroll = None
        if self.navigation.pending:
            if self.navigation.pending[0] < common:
                if pending_scroll == self.navigation.pending[0]:
                    self._pending_scroll = pending_scroll
            else:
                self.navigation.pending = None
        self._cancel_build()
        self._display_common = common if self._display_common is None else min(self._display_common, common)
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

    def reset_turns(self, turns):
        """Switch documents; never retain clickable rows from the old one."""
        self.clear_rows()
        self.items, self._display_items = turns, ()
        self._active = None
        self.prefix = [0]
        self.visible_rows = range(0)
        self.viewport_ready = False
        self.cancel_pending_scroll()
        self._display_common = None
        self.invalidate()
        if self.props.vadjustment:
            self.props.vadjustment.set_value(0)

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
        self._display_dirty = True
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
        self._retained_rows.clear()

    def close(self):
        self._closed = True
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

    def _anchor(self):
        anchor = self._pending_anchor
        if anchor is None and len(self.prefix) > 1:
            value = self._value()
            row = bisect_right(self.prefix, value) - 1
            anchor = row, value - self.prefix[row]
        if anchor is None or not self.items:
            return None
        row, offset = anchor
        if self._display_common is not None:
            # Reading rows are renumbered on every projection. Beyond the
            # common prefix, the old ordinal can refer to unrelated words.
            same_start = (
                row < min(len(self._display_items), len(self.items))
                and self._display_items[row].first_word == self.items[row].first_word
                and self._display_items[row].words[:1] == self.items[row].words[:1]
            )
            if row >= self._display_common and not same_start:
                row, offset = max(0, self._display_common - 1), 0
        return row, offset

    def _covers_anchor(self, index, height):
        if index.complete:
            return True
        anchor = self._anchor()
        if anchor is None:
            return False
        row, offset = anchor
        if row + 1 >= len(index.prefix):
            return False
        offset = min(offset, max(0, index.prefix[row + 1] - index.prefix[row] - 1))
        return index.prefix[row] + offset + height <= index.prefix[-1]

    def _activate(self, index):
        self._pending_anchor = self._anchor()
        self._display_common = None
        self._display_dirty = False
        self.viewport_ready = False
        focused = self.navigation.focused()
        for number, row in list(self.rows.items()):
            if number >= len(self.items) or row._turn != self.items[number]:
                if focused == number and self.navigation.pending is None:
                    self.navigation.pending = (min(number, len(self.items) - 1), None) if self.items else None
                removed = self.rows.pop(number)
                removed.close()
                removed.unparent()
                self.page.bound.pop(number, None)
            else:
                self.page._refresh_header(self.items[number], row)
                self.page._render_words(self.items[number], row.get_last_child())
                row.update_relation([Gtk.AccessibleRelation.SET_SIZE], [len(self.items)])
        self._retained_rows = set(self.rows)
        self._display_items = self.items
        self._active = index
        self.prefix = index.prefix
        self.layout_width = index.width

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
        if self._active is None or len(self.prefix) == 1:
            self._activate(index)
        if index.complete:
            return
        self._building = index
        if self.items:
            self._probe = self.page._build_row(self.items[0], register=False)
            self._probe.update_state([Gtk.AccessibleState.HIDDEN], [True])
            self._probe.set_focusable(False)
            self._probe.set_child_visible(False)
            self._probe.set_parent(self)
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
                markup = self.page._metric_transcript_markup(turn)
                self._metric_markup[number] = markup
            self.page._set_probe_turn(turn, self._probe, markup)
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

    def _display_row(self, index):
        row = self.rows.get(index)
        if row is None:
            row = self.page._build_row(self._display_items[index])
            following = next((self.rows[i] for i in sorted(self.rows) if i > index), None)
            row.insert_before(self, following)
            row.update_relation(
                [Gtk.AccessibleRelation.POS_IN_SET, Gtk.AccessibleRelation.SET_SIZE], [index + 1, len(self.items)]
            )
            self.rows[index] = row
        return row

    def _refresh_display_geometry(self, height):
        if len(self.prefix) <= 1:
            return
        value = self._value()
        anchor = bisect_right(self.prefix, value) - 1
        offset = value - self.prefix[anchor]
        if not isinstance(self.prefix, DisplayPrefix):
            self.prefix = self._active.prefix = DisplayPrefix(self.prefix, len(self._display_items))

        def measure(index):
            row = self._display_row(index)
            turn = self._display_items[index]
            self.page._refresh_header(turn, row)
            self.page._render_words(turn, row.get_last_child())
            natural = row.measure(Gtk.Orientation.VERTICAL, self.layout_width).natural
            if index + 1 == len(self.prefix):
                self.prefix.append(self.prefix[-1] + natural)
            else:
                self.prefix.set_height(index, natural)

        measured = {index for index in self.rows if index + 1 < len(self.prefix)}
        for index in measured:
            measure(index)
        end = anchor
        goal = self.prefix[anchor] + offset + height
        while end < len(self._display_items) and (end == anchor or self.prefix[end] < goal):
            if end not in measured:
                measure(end)
            end += 1
            goal = self.prefix[anchor] + min(offset, self.prefix[anchor + 1] - self.prefix[anchor] - 1) + height
        self._pending_anchor = anchor, offset

    def do_size_allocate(self, width, height, baseline):
        if self._closed or width <= 0 or height <= 0:
            return
        self._viewport_height = height
        target_width = min(width, self.column_cap)
        if self._requested_width != target_width:
            self._start_index(target_width)
        target = self._indices[target_width]
        if self._display_dirty and not target.complete:
            self._refresh_display_geometry(height)
        # Keep the displayed geometry until the replacement covers the current
        # viewport. Read the anchor now so hand scrolling during rebuilding wins.
        if self._active is not target and self._covers_anchor(target, height):
            self._activate(target)
        value = self._value()
        upper = max(height, self.prefix[-1])
        if self._pending_anchor is not None:
            row, offset = self._pending_anchor
            if row + 1 < len(self.prefix):
                offset = min(offset, max(0, self.prefix[row + 1] - self.prefix[row] - 1))
                goal = self.prefix[row] + offset
                # Wait for enough measured rows below the anchor to keep GTK
                # from clamping it, then restore it once.
                if goal <= upper - height or self._active.complete:
                    value = goal
                    self._pending_anchor = None
        adjustment = self.props.vadjustment
        if adjustment:
            adjustment.configure(value, 0, upper, 40, height * 0.9, height)
        if self.props.hadjustment:
            self.props.hadjustment.configure(0, 0, width, 40, width * 0.9, width)
        if (
            self._display_common is None
            and self._pending_scroll is not None
            and self._pending_scroll + 1 < len(self.prefix)
        ):
            index = self._pending_scroll
            self._pending_scroll = None
            self.scroll_to(index)
        value = self._value()
        previous_visible = self.visible_rows
        self.visible_rows = range(
            max(0, bisect_right(self.prefix, value) - 1),
            min(len(self.prefix) - 1, bisect_left(self.prefix, value + height)),
        )
        first, end = self.visible_rows.start, self.visible_rows.stop
        if self._active.complete:
            # Keep a small buffer once indexing settles. While the extent
            # grows, spending a frame on offscreen widgets delays visible text.
            first = max(0, min(first - 1, bisect_right(self.prefix, value - height / 2) - 1))
            end = min(len(self.prefix) - 1, max(end + 1, bisect_right(self.prefix, value + height * 1.5)))
        needed = set(range(first, end))
        if self._active.complete:
            self._retained_rows.clear()
        else:
            # Keep the pre-rebuild widgets until the replacement prefix is
            # complete. Header refreshes preserve their signal/focus identity.
            needed.update(self._retained_rows & self.rows.keys())
        focused = self.navigation.focused()
        if focused is not None:
            if focused + 1 < len(self.prefix):
                needed.add(focused)
            else:
                self.navigation.pending = focused, None
        if self.navigation.pending and self.navigation.pending[0] + 1 < len(self.prefix):
            needed.add(self.navigation.pending[0])
        for index in self.rows.keys() - needed:
            row = self.rows.pop(index)
            self.page.bound.pop(index, None)
            row.close()
            row.unparent()
        for index in sorted(needed):
            row = self.rows.get(index)
            if index + 1 >= len(self.prefix):
                row.set_child_visible(False)
                continue
            if row is None:
                row = self._display_row(index)
            elif index in self.visible_rows and index not in previous_visible:
                # Overscan keeps widgets ready, but only visible text tracks
                # playback ticks. Refresh a retained row before it paints.
                turn, words, _ = self.page.bound[index]
                self.page._render_words(turn, words)
            row.set_child_visible(True)
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

    def cancel_pending_scroll(self):
        self._pending_scroll = None
        self._pending_anchor = None
        self.navigation.pending = None

    def scroll_to(self, index, *_args):
        if not 0 <= index < len(self.items):
            return
        self._pending_anchor = None
        self._pending_scroll = None
        if self._display_common is not None or index + 1 >= len(self.prefix):
            self._pending_scroll = index
            return
        if 0 <= index < len(self.items) and self.props.vadjustment:
            self.props.vadjustment.clamp_page(self.prefix[index], self.prefix[index + 1])

    def do_snapshot(self, snapshot):
        for row in self.rows.values():
            if row.get_child_visible():
                self.snapshot_child(row, snapshot)
