# SPDX-License-Identifier: GPL-3.0-or-later
"""Position readout, cached turn ticks and pointer-only six-pixel snapping."""

from bisect import bisect_right
from heapq import heappop, heappush

from gi.repository import Gdk, Graphene, Gtk

from . import ux_formats as fmt
from .strings import text


def time_index(turns):
    """Latest-starting audible speaker at each boundary, including overlaps."""
    starts = {}
    boundaries = set()
    for index, turn in enumerate(turns):
        if turn.end_ms <= turn.start_ms:
            continue
        starts.setdefault(turn.start_ms, []).append((-turn.start_ms, -index, turn.end_ms, turn.speaker))
        boundaries.update((turn.start_ms, turn.end_ms))
    positions, speakers, active = [], [], []
    for at in sorted(boundaries):
        for value in starts.get(at, ()):
            heappush(active, value)
        while active and active[0][2] <= at:
            heappop(active)
        speaker = active[0][3] if active else None
        if not speakers or speakers[-1] != speaker:
            positions.append(at)
            speakers.append(speaker)
    return tuple(positions), tuple(speakers)


def snap_start(turns, x, left, width, duration):
    if not turns or width <= 0 or duration <= 0:
        return None
    closest = min(turns, key=lambda turn: abs(left + turn.start_ms * width / duration - x))
    distance = abs(round(left + closest.start_ms * width / duration) - x)
    return closest.start_ms if distance <= 6 else None


class TurnTicks(Gtk.Widget):
    """GSK rectangles avoid adding a Cairo runtime dependency for tiny marks."""

    def __init__(self, timeline):
        super().__init__(can_target=False)
        self.timeline = timeline

    def do_snapshot(self, snapshot):
        self.timeline.draw_ticks(snapshot, self.get_height())


class ListeningTimeline(Gtk.Overlay):
    def __init__(self, page, scale):
        super().__init__(hexpand=True)
        self.page, self.scale = page, scale
        self.set_child(scale)
        self.ticks = TurnTicks(self)
        self.add_overlay(self.ticks)
        self._turns = None
        self._target = ()
        self._positions, self._speakers = (), ()
        self._dragging = self._snapped = self._dragged = False
        self._press_x = self._press_y = 0
        self._snap_position = None
        self._x = 0
        scale.update_property([Gtk.AccessibleProperty.LABEL], [text("player.position")])
        scale.set_has_tooltip(True)
        scale.connect("query-tooltip", self._tooltip)
        events = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        events.connect("event", self._event)
        scale.add_controller(events)
        self.readout = Gtk.Label(margin_top=6, margin_bottom=6, margin_start=8, margin_end=8)
        self.drag_popup = Gtk.Popover(child=self.readout, autohide=False, focusable=False, can_target=False)
        self.drag_popup.set_parent(scale)

    def set_turns(self, turns):
        if turns != self._turns:
            self._turns = turns
            self._positions, self._speakers = time_index(turns)

    def set_target(self, turns):
        if turns != self._target:
            self._target = turns
            self.ticks.queue_draw()

    def readout_at(self, position):
        index = bisect_right(self._positions, position) - 1
        speaker_id = self._speakers[index] if index >= 0 else None
        speaker = next((s for s in self.page.meeting.speakers if s.id == speaker_id), None)
        return (
            text("player.timeline.tooltip", position=fmt.position(position), name=speaker.name)
            if speaker
            else fmt.position(position)
        )

    def _geometry(self):
        rect = self.scale.get_range_rect()
        start, end = self.scale.get_slider_range()
        half = (end - start) / 2
        return rect.x + half, max(0, rect.width - 2 * half)

    def position_at(self, x):
        left, width = self._geometry()
        duration = self.scale.get_adjustment().get_upper()
        return round(max(0, min(1, (x - left) / max(1, width))) * duration)

    def _tooltip(self, _scale, x, _y, keyboard, tooltip):
        position = self.page.player.position if keyboard else self.position_at(x)
        tooltip.set_text(self.readout_at(position))
        return True

    def _show_drag(self, x):
        position = self._snap_position if self._snapped and not self._dragged else self.position_at(x)
        self.readout.set_text(self.readout_at(position))
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = round(x), 0, 1, 1
        self.drag_popup.set_pointing_to(rect)
        self.drag_popup.popup()

    def _event(self, _controller, event):
        kind = event.get_event_type()
        if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.MOTION_NOTIFY):
            ok, x, y = event.get_position()
            if not ok:
                return False
            native = self.scale.get_native()
            dx, dy = native.get_surface_transform()
            point = Graphene.Point()
            point.init(x + dx, y + dy)
            ok, local = native.compute_point(self.scale, point)
            if not ok:
                return False
            self._x = local.x
            y = local.y
        if kind == Gdk.EventType.BUTTON_PRESS and event.get_button() == 1:
            self._dragging = True
            self._dragged = False
            self._press_x, self._press_y = self._x, y
            left, width = self._geometry()
            start = snap_start(self._target, self._x, left, width, self.scale.get_adjustment().get_upper())
            self._snapped = start is not None
            self._snap_position = start
            if self._snapped:
                self.page.player.seek(start)
            self._show_drag(self._x)
            return self._snapped
        if kind == Gdk.EventType.MOTION_NOTIFY and self._dragging:
            if self._snapped:
                self._dragged |= self.scale.drag_check_threshold(
                    round(self._press_x), round(self._press_y), round(self._x), round(y)
                )
                if self._dragged:
                    self.page.player.seek(self.position_at(self._x))
            self._show_drag(self._x)
        if kind == Gdk.EventType.BUTTON_RELEASE and event.get_button() == 1:
            self._dragging = False
            if self._snapped and self._dragged:
                self.page.player.seek(self.position_at(self._x))
            self.drag_popup.popdown()
            snapped, self._snapped = self._snapped, False
            return snapped
        return False

    def draw_ticks(self, snapshot, height):
        duration = self.scale.get_adjustment().get_upper()
        if not duration or not self._target:
            return
        left, width = self._geometry()
        found, colour = self.scale.get_style_context().lookup_color("accent_color")
        if not found:
            colour = self.scale.get_color()
        for turn in self._target:
            x = left + turn.start_ms * width / duration
            rect = Graphene.Rect()
            rect.init(round(x) - 1, height / 2 - 4, 3, 8)
            snapshot.append_color(colour, rect)

    def position_changed(self):
        self.scale.update_property(
            [Gtk.AccessibleProperty.VALUE_TEXT],
            [text("player.position.value", position=fmt.position(self.page.player.position))],
        )

    def close(self):
        self.drag_popup.popdown()
        self.drag_popup.unparent()
