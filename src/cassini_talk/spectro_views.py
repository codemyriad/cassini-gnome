# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 mapped view lifetime: one frame callback and one feed per meeting/window."""

import logging
import math
import os
import time
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from gi.repository import GLib

from .spectro_transport import MeterClient
from .spectro_view_model import DisplayModel

LOG = logging.getLogger(__name__)


def monotonic_ms():
    return GLib.get_monotonic_time() / 1000


class NativeFeed:
    def __init__(self, recorder, columns, *, clock=monotonic_ms):
        self.recorder, self.columns, self.clock = recorder, columns, clock
        self.source = recorder.spectrogram
        self.subscription = self.source.subscribe(self.now())

    @property
    def done(self):
        return self.source.finished or self.source.problem is not None or self.subscription is None

    def now(self):
        return max(0, self.clock() - self.recorder._start_us / 1000)

    def sample(self):
        now = self.now()
        if self.subscription is not None:
            self.columns(now, self.source.drain(self.subscription, now))
        return now

    def close(self):
        if self.subscription is not None:
            self.source.unsubscribe(self.subscription)
            self.subscription = None


class BrokerFeed:
    def __init__(self, path, directory, columns, *, clock=monotonic_ms, client_factory=MeterClient):
        self.columns, self.clock = columns, clock
        self.origin = None
        self.last = 0
        self.done = False
        self.client = client_factory(path, directory, self._arrived, self._closed)

    def _arrived(self, now, lanes):
        # Both lanes share this session clock. The best observed IPC offset
        # avoids arrival jitter changing scroll speed or moving it backwards.
        origin = self.clock() - now
        self.origin = origin if self.origin is None else min(origin, self.origin)
        self.columns(self.sample(), lanes)

    def sample(self):
        if self.origin is not None:
            self.last = max(self.last, self.clock() - self.origin)
        return self.last

    def _closed(self, _client):
        self.done = True

    def close(self):
        self.client.close()
        self.done = True


@dataclass
class Binding:
    key: object
    factory: object
    events: tuple
    silent: bool
    active: bool
    stopping: bool
    handlers: tuple


class StoppedFeed:
    done = True

    def sample(self):
        return 0

    def close(self):
        pass


@dataclass
class SessionView:
    model: DisplayModel
    feed: object
    ended: bool = False
    events: tuple | None = None
    silent: bool | None = None


class WindowSpectrograms:
    """Owns mapped registrations; detached/hidden views own no analysis history.

    Factories receive model.ingest and return a NativeFeed or BrokerFeed. Keeping
    that choice outside the renderer also makes independent sessions and window
    visibility testable with injected clocks and frame events.
    """

    def __init__(self, window, *, enabled=None):
        self.window = window
        self.enabled = os.environ.get("CASSINI_SPECTRO") != "0" if enabled is None else enabled
        self.bindings = WeakKeyDictionary()
        self.sessions = {}
        self.tick_id = 0
        self.in_tick = False
        self.closed = False
        self.dark = self.reduced_motion = False
        self.last_pill = None
        self.frames = self.tick_ns = 0
        self.handlers = [window.connect(signal, self._changed) for signal in ("map", "unmap", "notify::suspended")]

    def bind(self, widget, key, factory, *, events=(), silent=False, active=True, stopping=False):
        if self.closed:
            return
        existing = self.bindings.get(widget)
        events = tuple(events)
        if existing and (existing.key, existing.events, existing.silent, existing.active, existing.stopping) == (
            key,
            events,
            silent,
            active,
            stopping,
        ):
            existing.factory = factory
            return
        handlers = (
            existing.handlers
            if existing
            else tuple(widget.connect(signal, self._changed) for signal in ("map", "unmap"))
        )
        if existing and existing.key != key:
            widget.clear()
        events = tuple(events)
        if (view := self.sessions.get(key)) and view.events == events:
            events = view.events
        self.bindings[widget] = Binding(key, factory, events, silent, active, stopping, handlers)
        widget.configure(dark=self.dark, reduced_motion=self.reduced_motion)
        widget.set_stopping(stopping)
        self._sync()

    def remove(self, widget):
        if binding := self.bindings.pop(widget, None):
            for handler in binding.handlers:
                widget.disconnect(handler)
        widget.clear()
        self._sync()

    def configure(self, *, dark, reduced_motion):
        self.dark, self.reduced_motion = dark, reduced_motion
        for widget in self.bindings:
            widget.configure(dark=dark, reduced_motion=reduced_motion)

    def _changed(self, *_args):
        self._sync()

    def _visible(self):
        return self.enabled and not self.closed and self.window.get_mapped() and not self.window.is_suspended()

    def _stop_tick(self):
        if self.tick_id:
            identifier, self.tick_id = self.tick_id, 0
            if not self.in_tick:
                self.window.remove_tick_callback(identifier)

    def _sync(self):
        desired = {}
        visible = self._visible()
        for widget, binding in list(self.bindings.items()):
            if visible and widget.get_mapped() and binding.active and binding.key is not None:
                desired.setdefault(binding.key, binding)
            elif not visible or not widget.get_mapped():
                widget.clear()
        for key in self.sessions.keys() - desired.keys():
            self.sessions.pop(key).feed.close()
        for key, binding in desired.items():
            if key not in self.sessions:
                model = DisplayModel()
                try:
                    feed = StoppedFeed() if binding.stopping else binding.factory(model.ingest)
                except Exception:
                    # A picture never prevents controls, capture or saved audio.
                    LOG.exception("Could not subscribe to the recording sound indicator")
                    feed = StoppedFeed()
                self.sessions[key] = SessionView(model, feed)
            view = self.sessions[key]
            if view.events is not binding.events:
                if view.events != binding.events:
                    view.model.set_events(binding.events)
                view.events = binding.events
            if not view.ended:
                if view.silent != binding.silent:
                    view.model.set_warning(view.feed.sample(), binding.silent)
                    view.silent = binding.silent
                if view.feed.done or binding.stopping:
                    view.model.advance(view.feed.sample())
                    view.feed.close()
                    view.ended = True
                    for widget, item in list(self.bindings.items()):
                        if item.key == key and widget.get_mapped():
                            widget.update(view.model, animate=False)
        ticking = any(not view.ended for view in self.sessions.values())
        if ticking and not self.tick_id:
            self.last_pill = None
            self.tick_id = self.window.add_tick_callback(self._tick)
        elif not ticking:
            self._stop_tick()

    def _tick(self, _window, frame_clock):
        started = time.perf_counter_ns()
        self.in_tick = True
        try:
            if not self.tick_id:
                return False
            now = frame_clock.get_frame_time() / 1000
            pill = math.floor(now * 0.03)
            pill_due = pill != self.last_pill
            self.last_pill = pill
            if not pill_due and not any(
                widget.kind == "row" and widget.get_mapped() and binding.active
                for widget, binding in self.bindings.items()
            ):
                return True
            for view in self.sessions.values():
                if not view.ended:
                    view.model.advance(view.feed.sample())
                    if view.feed.done:
                        view.feed.close()
                        view.ended = True
            for widget, binding in list(self.bindings.items()):
                if not widget.get_mapped() or binding.key not in self.sessions or not binding.active:
                    continue
                if widget.kind == "row" or pill_due:
                    view = self.sessions[binding.key]
                    widget.update(view.model, animate=not view.ended)
            self.frames += 1
            if not any(not view.ended for view in self.sessions.values()):
                self._stop_tick()
            return bool(self.tick_id)
        except Exception:
            LOG.exception("Recording sound indicator stopped; saved audio is retained")
            self.close()
            return False
        finally:
            self.in_tick = False
            self.tick_ns += time.perf_counter_ns() - started

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._stop_tick()
        for view in self.sessions.values():
            view.feed.close()
        self.sessions.clear()
        for widget in list(self.bindings):
            self.remove(widget)
        for handler in self.handlers:
            self.window.disconnect(handler)
        self.handlers.clear()
