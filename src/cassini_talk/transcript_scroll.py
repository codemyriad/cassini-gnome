# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep virtual transcript remeasurement out of the kinetic motion origin."""

from gi.repository import Gdk, GLib, Gtk

from .kinetic_scroll import KineticMotion


class TranscriptScroll:
    def __init__(self, scroller, manual_scroll):
        self.scroller = scroller
        self.manual_scroll = manual_scroll
        self.motion = None
        self.tick = 0
        scroller.set_kinetic_scrolling(False)
        self.touch_drag = Gtk.GestureDrag(touch_only=True, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        self.touch_swipe = Gtk.GestureSwipe(touch_only=True, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        self.touch_drag.connect("drag-begin", self._touch_begin)
        self.touch_drag.connect("drag-update", self._touch_update)
        self.touch_swipe.connect("swipe", self._touch_swipe)
        scroller.add_controller(self.touch_drag)
        scroller.add_controller(self.touch_swipe)
        self.touch_drag.group(self.touch_swipe)
        self._touch_y = 0
        self.controller = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES | Gtk.EventControllerScrollFlags.KINETIC,
            propagation_phase=Gtk.PropagationPhase.CAPTURE,
        )
        self.controller.connect("scroll-begin", lambda *_: self.stop())
        self.controller.connect("scroll", self._scroll)
        self.controller.connect("decelerate", self._decelerate)
        scroller.add_controller(self.controller)
        scroller.connect("unmap", lambda *_: self.stop())
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        scroller.add_controller(keys)
        click = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_: self.stop())
        scroller.add_controller(click)
        drag = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        for signal in ("drag-begin", "drag-update", "drag-end"):
            drag.connect(signal, self._drag)
        scroller.get_vscrollbar().add_controller(drag)

    def stop(self):
        if self.tick:
            self.scroller.remove_tick_callback(self.tick)
            self.tick = 0
        self.motion = None

    def _drag(self, *_):
        self.stop()
        self.manual_scroll()

    def _touch_begin(self, _gesture, _x, _y):
        self.stop()
        self._touch_y = 0
        self.manual_scroll()

    def _touch_update(self, gesture, _dx, dy):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        adjustment = self.scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_value() + self._touch_y - dy)
        self._touch_y = dy

    def _touch_swipe(self, _gesture, _vx, vy):
        self._start_motion(-vy)

    def _key(self, _controller, key, _code, state):
        if state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if key in (
            Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down, Gdk.KEY_Home, Gdk.KEY_End,
            Gdk.KEY_KP_Up, Gdk.KEY_KP_Down, Gdk.KEY_KP_Page_Up, Gdk.KEY_KP_Page_Down,
            Gdk.KEY_KP_Home, Gdk.KEY_KP_End,
        ):
            self.stop()
            self.manual_scroll()
        return False

    def _factor(self, controller):
        if controller.get_unit() == Gdk.ScrollUnit.WHEEL:
            return self.scroller.get_vadjustment().get_page_size() ** (2 / 3)
        return 2.5

    def _scroll(self, controller, _dx, dy):
        self.stop()
        self.manual_scroll()
        if not dy or controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
            return False
        adjustment = self.scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_value() + dy * self._factor(controller))
        return True

    def _decelerate(self, controller, _vx, vy):
        self.stop()
        if (
            not vy
            or not self.scroller.get_mapped()
            or controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK
        ):
            return
        self.manual_scroll()
        self._start_motion(vy * self._factor(controller))

    def _start_motion(self, velocity):
        self.stop()
        if not velocity or not self.scroller.get_mapped():
            return
        clock = self.scroller.get_frame_clock()
        self.motion = KineticMotion(velocity, clock.get_frame_time())
        self.tick = self.scroller.add_tick_callback(self._frame)

    def _frame(self, _widget, clock):
        motion = self.motion
        if not self.scroller.get_mapped() or motion is None:
            self.tick = 0
            self.motion = None
            return GLib.SOURCE_REMOVE
        adjustment = self.scroller.get_vadjustment()
        before = adjustment.get_value()
        delta = motion.advance(clock.get_frame_time())
        # GTK can rebase pixels to keep a row anchored after measuring it.
        # Never replay an absolute position calculated before that correction.
        adjustment.set_value(before + delta)
        if not motion.active or (delta and adjustment.get_value() == before):
            self.tick = 0
            self.motion = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
