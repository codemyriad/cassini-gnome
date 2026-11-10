# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Time-boxed real-height transcript prototype. Not a shipping renderer."""

import bisect
import time

from gi.repository import GLib, Gtk


class SpikeWords(Gtk.Label):
    """Avoid GTK's glyph-per-line minimum request; retain real wrapped height."""

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL and for_size < 0:
            layout = self.get_layout().copy()
            layout.set_width(-1)
            natural, _height = layout.get_pixel_size()
            return 0, natural, -1, -1
        minimum, natural, baseline, natural_baseline = Gtk.Label.do_measure(self, orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            baseline = natural_baseline = -1
        return minimum, natural, baseline, natural_baseline


class RowItem:
    def __init__(self, item):
        self.item, self.child = item, None

    def get_item(self):
        return self.item

    def get_child(self):
        return self.child

    def set_child(self, child):
        self.child = child


class RowBuild:
    """Same row widgets, top-down idle construction, no height estimates."""

    def _init_model(self, page, store):
        self.page, self.store = page, store
        self.items = []
        self.built = False
        self.pending_scroll = None
        self.source = 0
        self.build_cpu_ms = []
        self.visible_refresh_cpu_ms = []
        self.handler = store.connect("items-changed", self._changed)
        self._changed(store, 0, 0, store.get_n_items())
        self.connect("map", self._mapped)

    def set_enable_rubberband(self, _enabled):
        pass

    def _mapped(self, *_):
        if not hasattr(self, "adjustment_handler"):
            adjustment = self.page.scroller.get_vadjustment()
            self.adjustment_handler = adjustment.connect("value-changed", lambda *_: self.refresh_position())
            self.geometry_handler = adjustment.connect("changed", self._queue_visible_refresh)

    def _queue_visible_refresh(self, *_):
        if not self.built:
            return
        if not getattr(self, "refresh_source", 0):
            self.refresh_source = GLib.idle_add(self._visible_refresh)

    def _visible_refresh(self):
        self.refresh_source = 0
        self.refresh_position()
        return GLib.SOURCE_REMOVE

    def _changed(self, _store, position, _removed, _added):
        self.built = False
        # Keep the unchanged prefix, matching MeetingPage.update's splice.
        while len(self.items) > position:
            item = self.items.pop()
            child = item.child
            parent = child.get_parent()
            self.page._unbind(None, item)
            self.remove(child if parent is self else parent)
        if not self.source:
            self.source = GLib.idle_add(self._build, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _build(self):
        if self.page._closed:
            self.source = 0
            return GLib.SOURCE_REMOVE
        started = time.thread_time_ns()
        while len(self.items) < self.store.get_n_items():
            item = RowItem(self.store.get_item(len(self.items)))
            self.page._bind(None, item)
            self.append(item.child)
            self.items.append(item)
            if time.thread_time_ns() - started >= 3_000_000:
                break
        self.build_cpu_ms.append((time.thread_time_ns() - started) / 1e6)
        self.built = len(self.items) == self.store.get_n_items()
        if self.pending_scroll is not None and self.pending_scroll < len(self.items):
            target = self.pending_scroll
            self.pending_scroll = None
            self.add_tick_callback(lambda *_: (self.scroll_to(target), GLib.SOURCE_REMOVE)[1])
        if self.built:
            self.source = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def visible_indices(self):
        adjustment = self.page.scroller.get_vadjustment()
        first = self.get_row_at_y(round(adjustment.get_value())) or self.get_row_at_index(0)
        last = self.get_row_at_y(round(adjustment.get_value() + adjustment.get_page_size()))
        if first is None:
            return
        stop = last.get_index() + 1 if last else len(self.items)
        for index in range(first.get_index(), stop):
            row = self.get_row_at_index(index)
            if row is None or row.get_height() <= 0:
                break
            ok, bounds = row.compute_bounds(self.page.scroller)
            if not ok:
                continue
            if bounds.get_y() >= self.page.scroller.get_height():
                break
            if bounds.get_y() + bounds.get_height() > 0:
                yield index

    def refresh_position(self):
        if self.page._closed:
            return
        started = time.thread_time_ns()
        # Off-screen labels need no visual highlight mutation. They are
        # refreshed when scrolling/resize reveals them; avoid invalidating a
        # whole 2,000-row scene for an inaudible-to-the-reader off-screen word.
        for index in self.visible_indices():
            entry = self.page.bound.get(index)
            if entry:
                self.page._render_words(entry[0], entry[1])
        self.visible_refresh_cpu_ms.append((time.thread_time_ns() - started) / 1e6)

    def scroll_to(self, index, *_args):
        row = self.get_row_at_index(index)
        if row is None:
            self.pending_scroll = index
            return
        if row.get_height() <= 0:
            self.pending_scroll = index
            if not getattr(self, "scroll_tick", 0):
                self.scroll_tick = self.add_tick_callback(self._allocated_scroll)
            return
        ok, bounds = row.compute_bounds(self)
        if ok:
            self.page.scroller.get_vadjustment().clamp_page(bounds.get_y(), bounds.get_y() + bounds.get_height())

    def _allocated_scroll(self, *_):
        target = self.pending_scroll
        row = self.get_row_at_index(target) if target is not None else None
        if row is None or row.get_height() <= 0:
            return GLib.SOURCE_CONTINUE
        self.pending_scroll = None
        self.scroll_tick = 0
        self.scroll_to(target)
        return GLib.SOURCE_REMOVE

    def stop(self):
        if getattr(self, "refresh_source", 0):
            GLib.source_remove(self.refresh_source)
            self.refresh_source = 0
        if getattr(self, "scroll_tick", 0):
            self.remove_tick_callback(self.scroll_tick)
            self.scroll_tick = 0
        if self.source:
            GLib.source_remove(self.source)
            self.source = 0
        self.store.disconnect(self.handler)
        for item in reversed(self.items):
            child = item.child
            parent = child.get_parent()
            self.page._unbind(None, item)
            self.remove(child if parent is self else parent)
        self.items.clear()
        if hasattr(self, "adjustment_handler"):
            self.page.scroller.get_vadjustment().disconnect(self.adjustment_handler)
            self.page.scroller.get_vadjustment().disconnect(self.geometry_handler)


class RealRows(Gtk.ListBox, RowBuild):
    def __init__(self, page, store):
        Gtk.ListBox.__init__(self, selection_mode=Gtk.SelectionMode.NONE, css_classes=["meeting-transcript"])
        self._init_model(page, store)


class BoxRows(Gtk.Box, RowBuild):
    """Same real row widgets, without ListBox's per-row bookkeeping.

    Positions are read from completed native allocations, never estimated or
    used to allocate widgets. GTK Box and Viewport own layout and scrolling.
    """

    def __init__(self, page, store):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, css_classes=["meeting-transcript"])
        self.positions = []
        self.positions_width = None
        self._init_model(page, store)

    def append(self, child):
        index = len(self.items)
        child.get_index = lambda: index
        Gtk.Box.append(self, child)

    def _changed(self, store, position, removed, added):
        self.positions = self.positions[:position]
        RowBuild._changed(self, store, position, removed, added)

    def _mapped(self, *args):
        RowBuild._mapped(self, *args)
        if not hasattr(self, "box_changed_handler"):
            self.box_changed_handler = self.page.scroller.get_vadjustment().connect("changed", self._geometry_changed)

    def _geometry_changed(self, *_):
        if self.built:
            self.positions = []

    def get_row_at_index(self, index):
        return self.items[index].child if 0 <= index < len(self.items) else None

    def get_row_at_y(self, y):
        if self.positions_width != self.get_width():
            self.positions = []
            self.positions_width = self.get_width()
        for index in range(len(self.positions), len(self.items)):
            child = self.items[index].child
            if child.get_height() <= 0:
                break
            ok, bounds = child.compute_bounds(self)
            if not ok:
                break
            self.positions.append(bounds.get_y())
        if not self.positions:
            return None
        index = max(0, bisect.bisect_right(self.positions, y) - 1)
        return self.items[index].child

    def stop(self):
        if hasattr(self, "box_changed_handler"):
            self.page.scroller.get_vadjustment().disconnect(self.box_changed_handler)
        RowBuild.stop(self)
