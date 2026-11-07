# SPDX-License-Identifier: GPL-3.0-or-later
"""Position readout, cached turn ticks and pointer-only six-pixel snapping."""

from bisect import bisect_right

from gi.repository import Gdk, Graphene, Gtk

from . import ux_formats as fmt
from .listening_timeline_model import snap_start, time_index
from .strings import text


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
        self._drag_geometry = None
        self._x = 0
        scale.update_property([Gtk.AccessibleProperty.LABEL], [text("player.position")])
        scale.set_has_tooltip(True)
        scale.connect("query-tooltip", self._tooltip)
        self.gesture = Gtk.GestureDrag(button=1, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        self.gesture.connect("drag-begin", self._begin)
        self.gesture.connect("drag-update", self._drag)
        self.gesture.connect("drag-end", self._end)
        self.gesture.connect("cancel", self._cancel)
        scale.add_controller(self.gesture)
        self._closed = False
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
        speaker = self.page.meeting.speaker_for(speaker_id) if speaker_id is not None else None
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
        left, width = self._drag_geometry if self._dragging else self._geometry()
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

    def _begin(self, gesture, x, y):
        if self._closed:
            return
        # Typed gesture coordinates are already local to the scale. The
        # legacy Gdk.Event signal is not marshalled on all supported GI builds.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.scale.grab_focus()
        self._drag_geometry = self._geometry()
        self._dragging, self._dragged = True, False
        self._press_x, self._press_y = x, y
        left, width = self._drag_geometry
        start = snap_start(self._target, x, left, width, self.scale.get_adjustment().get_upper())
        self._snapped = start is not None
        self._snap_position = start
        self.page.player.seek(start if self._snapped else self.position_at(x))
        self._show_drag(x)

    def _drag(self, _gesture, dx, dy):
        if self._closed or not self._dragging:
            return
        self._x = self._press_x + dx
        self._dragged |= self.scale.drag_check_threshold(
            round(self._press_x), round(self._press_y), round(self._x), round(self._press_y + dy)
        )
        if not self._snapped or self._dragged:
            self.page.player.seek(self.position_at(self._x))
        self._show_drag(self._x)

    def _end(self, gesture, dx, dy):
        if not self._closed and self._dragging:
            self._drag(gesture, dx, dy)
            self._dragging = False
            self.drag_popup.popdown()

    def _cancel(self, *_):
        self._dragging = False
        if not self._closed:
            self.drag_popup.popdown()

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
        if self._closed:
            return
        self._closed = True
        self.drag_popup.popdown()
        self.drag_popup.unparent()
