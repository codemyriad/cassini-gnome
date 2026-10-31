# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 ring textures: a few GSK draws per frame, no Cairo or cell drawing."""

import math
import time
from functools import cache

from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk

from .spectro_view_model import HOLE, OFF, SILENT


def rect(x, y, width, height):
    result = Graphene.Rect()
    result.init(x, y, width, height)
    return result


@cache
def palette(kind, dark, silent):
    ink = ("ff8a8e" if dark else "c01c28") if kind == "pill" else ("ffffff" if dark else "2e2e32")
    background = ("f8e45c" if dark else ("e5a50a" if kind == "pill" else "c58a00")) if silent else ink
    rgb = [int(ink[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    bg = [int(background[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    base = (0.26 if dark else 0.42) if kind == "pill" else (0.18 if dark else 0.26)
    if not silent:
        base = 0.13 if kind == "pill" else 0.06
    maximum = 0.95 if kind == "pill" else (0.88 if dark else 0.90)
    result = []
    for value in range(256):
        alpha = maximum * (value / 255) ** 1.35
        total = alpha + base * (1 - alpha)
        result.append(
            bytes([*(round(255 * (rgb[i] * alpha + bg[i] * base * (1 - alpha))) for i in range(3)), round(total * 255)])
        )
    opacity = 0.55 if kind == "pill" else 0.40
    dot = bytes([*(round(255 * value * opacity) for value in rgb), round(255 * opacity)])
    return result, dot


class SpectrogramStrip(Gtk.Widget):
    def __init__(self, *, kind="row", lanes=("me",), dark=False, reduced_motion=False):
        super().__init__(accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.kind, self.lanes = kind, lanes
        self.columns, self.rows = (50, 16) if kind == "pill" else (200, 32)
        self.dark, self.reduced_motion = dark, reduced_motion
        self.model = self.texture = self.pixels = None
        self.last = None
        self.display = None
        self.level = 0
        self.texture_updates = self.snapshots = self.snapshot_ns = self.snapshot_cpu_ns = 0
        self.set_size_request(self.columns, 20 if kind == "pill" else 32)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self._stops = []
        for offset, alpha in ((0, 0), (1, 1)):
            colour = Gdk.RGBA()
            colour.red = colour.green = colour.blue = 1
            colour.alpha = alpha
            stop = Gsk.ColorStop()
            stop.offset, stop.color = offset, colour
            self._stops.append(stop)

    @property
    def capacity(self):
        return self.columns if self.reduced_motion else self.columns + 2

    def set_fraction(self, value):
        # Existing AC levels retain their meaning; bands never feed decisions.
        self.level = max(0, min(1, value))

    def get_fraction(self):
        return self.level

    def set_stopping(self, stopping):
        (self.add_css_class if stopping else self.remove_css_class)("spectro-stopping")

    def clear(self):
        self.model = self.texture = self.pixels = self.last = self.display = None

    def configure(self, *, dark, reduced_motion):
        if (dark, reduced_motion) == (self.dark, self.reduced_motion):
            return
        self.dark, self.reduced_motion = dark, reduced_motion
        self.texture = self.pixels = self.last = None
        if self.model is not None:
            self.update(self.model)

    def update(self, model, *, animate=True):
        self.model = model
        latest = model.latest_pill if self.kind == "pill" else model.last
        if latest is None:
            return False
        changed = latest != self.last or self.pixels is None
        if changed:
            self._bake(latest)
        display = model.display_ms - (60 if self.kind == "pill" else 0)
        moved = self.display != display and animate and not self.reduced_motion
        self.display = display
        if changed or moved:
            self.queue_draw()
        return changed or moved

    def _bake(self, latest):
        capacity, height = self.capacity, self.rows * len(self.lanes)
        data = bytearray(self.pixels.get_data()) if self.pixels else bytearray(capacity * height * 4)
        first = latest - capacity + 1 if self.last is None else max(self.last + 1, latest - capacity + 1)
        for slot in range(first, latest + 1):
            column = slot % capacity
            for lane, side in enumerate(self.lanes):
                flags, bands = self.model.pill_cell(side, slot) if self.kind == "pill" else self.model.cell(side, slot)
                lut, dot = palette(self.kind, self.dark, bool(flags & SILENT))
                for row in range(self.rows):
                    if flags == HOLE:
                        pixel = bytes(4)
                    elif flags & OFF:
                        pixel = dot if slot % 3 == 0 and row == self.rows // 2 else bytes(4)
                    else:
                        pixel = lut[bands[self.rows - row - 1]]
                    index = ((lane * self.rows + row) * capacity + column) * 4
                    data[index : index + 4] = pixel
        if self.reduced_motion:
            for slot in (latest + 1, latest + 2):
                for row in range(height):
                    index = (row * capacity + slot % capacity) * 4
                    data[index : index + 4] = bytes(4)
        # PyGObject's bytes fast path copies the packed buffer. A bytearray is
        # marshalled element by element and dominates the entire frame budget.
        self.pixels = GLib.Bytes.new(bytes(data))
        self.texture = Gdk.MemoryTexture.new(
            capacity, height, Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED, self.pixels, capacity * 4
        )
        self.last = latest
        self.texture_updates += 1

    def do_snapshot(self, snapshot):
        if self.texture is None:
            return
        started, cpu_started = time.perf_counter_ns(), time.thread_time_ns()
        width, height = self.get_width(), self.get_height()
        if not self.reduced_motion:
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
            start, end = Graphene.Point(), Graphene.Point()
            start.init(0, 0)
            end.init(width * 0.16, 0)
            snapshot.append_linear_gradient(rect(0, 0, width, height), start, end, self._stops)
            snapshot.pop()
        gap = 2 if len(self.lanes) > 1 else 0
        lane_height = (height - gap * (len(self.lanes) - 1)) / len(self.lanes)
        for lane in range(len(self.lanes)):
            y = lane * (lane_height + gap)
            clip = Gsk.RoundedRect()
            clip.init_from_rect(rect(0, y, width, lane_height), 2 if self.kind == "pill" else 5)
            snapshot.push_rounded_clip(clip)
            if self.reduced_motion:
                positions = (0,)
            else:
                clock = self.display / (80 if self.kind == "pill" else 20)
                epoch = math.floor(clock / self.capacity) * self.capacity
                left = width - (clock - epoch) * width / self.columns
                positions = (left, left - self.capacity * width / self.columns)
            for left in positions:
                snapshot.append_scaled_texture(
                    self.texture,
                    Gsk.ScalingFilter.LINEAR,
                    rect(
                        left,
                        y - lane * lane_height,
                        self.capacity * width / self.columns,
                        lane_height * len(self.lanes),
                    ),
                )
            snapshot.pop()
        if not self.reduced_motion:
            snapshot.pop()
        self.snapshots += 1
        self.snapshot_ns += time.perf_counter_ns() - started
        self.snapshot_cpu_ns += time.thread_time_ns() - cpu_started
