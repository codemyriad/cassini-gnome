# SPDX-License-Identifier: GPL-3.0-or-later
"""Injected map/frame/clock events: no timers, desktops or audio devices."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
from cassini_talk.spectro_source import SpectroSource  # noqa: E402
from cassini_talk.spectro_views import BrokerFeed, NativeFeed, WindowSpectrograms  # noqa: E402


class Signals:
    def __init__(self):
        self.handlers = {}
        self.mapped = True

    def connect(self, name, callback):
        identifier = max(self.handlers, default=0) + 1
        self.handlers[identifier] = name, callback
        return identifier

    def disconnect(self, identifier):
        self.handlers.pop(identifier)

    def emit(self, name):
        for event, callback in list(self.handlers.values()):
            if event == name:
                callback(self)

    def get_mapped(self):
        return self.mapped


class Window(Signals):
    def __init__(self):
        super().__init__()
        self.suspended = False
        self.ticks = {}
        self.starts = self.removes = 0

    def is_suspended(self):
        return self.suspended

    def add_tick_callback(self, callback):
        self.starts += 1
        self.ticks[self.starts] = callback
        return self.starts

    def remove_tick_callback(self, identifier):
        self.removes += 1
        self.ticks.pop(identifier)

    def frame(self, ms):
        clock = SimpleNamespace(get_frame_time=lambda: round(ms * 1000))
        for identifier, callback in list(self.ticks.items()):
            if not callback(self, clock):
                self.ticks.pop(identifier, None)


class Strip(Signals):
    def __init__(self, kind="row"):
        super().__init__()
        self.kind, self.model = kind, None
        self.updates = []

    def configure(self, **style):
        self.style = style

    def update(self, model, *, animate=True):
        self.model = model
        self.updates.append(animate)

    def set_stopping(self, stopping):
        self.stopping = stopping

    def clear(self):
        self.model = None


class Feeds:
    def __init__(self):
        self.created = []
        self.now = 0

    def __call__(self, columns):
        feed = SimpleNamespace(done=False, closes=0, columns=columns, sample=lambda: self.now)

        def close():
            feed.done = True
            feed.closes += 1

        feed.close = close
        self.created.append(feed)
        return feed


def test_page_rows_and_pills_share_one_feed_model_and_window_callback():
    window, feeds = Window(), Feeds()
    hub = WindowSpectrograms(window)
    strips = [Strip("pill"), Strip(), Strip()]
    try:
        for strip in strips:
            hub.bind(strip, "same-meeting", feeds)
        assert len(feeds.created) == len(window.ticks) == window.starts == 1
        window.frame(100)
        assert all(strip.model is hub.sessions["same-meeting"].model for strip in strips)
        hub.remove(strips[0])
        assert feeds.created[0].closes == 0 and len(window.ticks) == 1
        hub.remove(strips[1])
        hub.remove(strips[2])
        assert feeds.created[0].closes == 1 and not window.ticks and not hub.sessions
    finally:
        hub.close()


@pytest.mark.parametrize("state", ["widget-unmapped", "window-unmapped", "suspended"])
def test_hidden_views_release_feeds_history_and_ticks_and_restart_cold(state):
    window, strip, feeds = Window(), Strip(), Feeds()
    hub = WindowSpectrograms(window)
    try:
        hub.bind(strip, "one", feeds)
        window.frame(100)
        old = strip.model
        if state == "widget-unmapped":
            strip.mapped = False
            strip.emit("unmap")
        elif state == "window-unmapped":
            window.mapped = False
            window.emit("unmap")
        else:
            window.suspended = True
            window.emit("notify::suspended")
        assert not window.ticks and not hub.sessions and strip.model is None
        assert feeds.created[0].closes == 1
        frames = hub.frames
        window.frame(100000)
        assert hub.frames == frames
        window.mapped = strip.mapped = True
        window.suspended = False
        window.emit("map")
        strip.emit("map")
        assert len(window.ticks) == 1 and len(feeds.created) == 2
        window.frame(100001)
        assert strip.model is not old
    finally:
        hub.close()


def test_separate_meetings_have_separate_feeds_and_changing_target_retires_only_its_feed():
    window, feeds = Window(), Feeds()
    hub = WindowSpectrograms(window)
    first, second = Strip(), Strip()
    try:
        hub.bind(first, "one", feeds)
        hub.bind(second, "two", feeds)
        assert len(feeds.created) == 2 and len(window.ticks) == 1
        hub.bind(first, "three", feeds)
        assert feeds.created[0].closes == 1 and feeds.created[1].closes == 0
        assert set(hub.sessions) == {"two", "three"}
    finally:
        hub.close()


def test_stopped_feed_unsubscribes_and_freezes_without_restarting_on_repeated_bind():
    window, strip, feeds = Window(), Strip(), Feeds()
    hub = WindowSpectrograms(window)
    try:
        hub.bind(strip, "one", feeds)
        window.frame(10)
        feeds.created[0].done = True
        window.frame(20)
        assert not window.ticks and feeds.created[0].closes == 1
        assert strip.model is hub.sessions["one"].model and strip.updates[-1] is False
        hub.bind(strip, "one", feeds)
        assert len(feeds.created) == 1 and not window.ticks
    finally:
        hub.close()


def test_pill_is_capped_at_thirty_updates_per_second_while_rows_follow_120hz():
    window, feeds = Window(), Feeds()
    hub = WindowSpectrograms(window)
    pill, row = Strip("pill"), Strip()
    try:
        for strip in (pill, row):
            hub.bind(strip, "one", feeds)
        for frame in range(240):
            feeds.now = frame * 1000 / 120
            window.frame(feeds.now)
        assert len(row.updates) == 240 and 59 <= len(pill.updates) <= 61
        assert len(window.ticks) == 1
    finally:
        hub.close()


def test_disabled_baseline_has_no_feed_or_frame_callback(monkeypatch):
    monkeypatch.setenv("CASSINI_SPECTRO", "0")
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    try:
        hub.bind(strip, "one", feeds)
        assert not feeds.created and not window.ticks and strip.model is None
    finally:
        hub.close()


def test_decoration_failure_releases_the_feed_and_all_handlers():
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Synthetic renderer failure")

    strip.update = fail
    hub.bind(strip, "one", feeds)
    window.frame(100)
    assert hub.closed and not window.ticks and not window.handlers and not strip.handlers
    assert feeds.created[0].closes == 1


def test_native_feed_subscribes_only_for_its_lifetime_and_keeps_session_clock():
    source = SpectroSource()
    source.declare("mic", kind="microphone")
    now, received = [1100], []
    recorder = SimpleNamespace(spectrogram=source, _start_us=1000000)
    feed = NativeFeed(recorder, lambda *args: received.append(args), clock=lambda: now[0])
    assert source.active and feed.sample() == 100
    now[0] += 20
    assert feed.sample() == 120 and received[-1][0] == 120
    feed.close()
    feed.close()
    assert feed.done and not source.active and not source.subscribers


def test_broker_clock_ignores_arrival_jitter_and_never_moves_backwards():
    class Client:
        def __init__(self, _path, _directory, columns, closed):
            self.columns, self.closed, self.stopped = columns, closed, False

        def close(self):
            self.stopped = True

    now, received = [1000], []
    feed = BrokerFeed(
        "socket", "meeting", lambda *args: received.append(args), clock=lambda: now[0], client_factory=Client
    )
    feed.client.columns(100, dict(me=[], others=[]))
    assert feed.sample() == 100
    now[0] = 1010
    feed.client.columns(105, dict(me=[], others=[]))
    assert feed.sample() == 110
    now[0] = 1001
    assert feed.sample() == 110
    feed.client.closed(feed.client)
    assert feed.done
    feed.close()
    assert feed.client.stopped


def test_unchanged_frames_do_not_rebuild_event_projection(monkeypatch):
    from cassini_talk.spectro_view_model import DisplayModel

    calls = []
    original = DisplayModel.set_events

    def project(model, events):
        calls.append(events)
        original(model, events)

    monkeypatch.setattr(DisplayModel, "set_events", project)
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    events = (dict(type="selection", atMs=0, me=True, others=False),)
    try:
        hub.bind(strip, "one", feeds, events=events)
        for frame in range(120):
            window.frame(frame * 1000 / 120)
        hub.bind(strip, "one", feeds, events=list(events))
        assert calls == [events]
        hub.bind(strip, "one", feeds, events=(*events, dict(type="selection", atMs=1000, me=True, others=True)))
        assert len(calls) == 2
    finally:
        hub.close()


def test_capture_finishing_during_the_frame_releases_its_subscription_before_tick_stops():
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    hub.bind(strip, "one", feeds)
    feed = feeds.created[0]

    def finish_while_sampling():
        feed.done = True
        return 100

    feed.sample = finish_while_sampling
    try:
        window.frame(100)
        assert feed.closes == 1, "Finishing between the phase check and frame sample must unsubscribe"
        assert not window.ticks and strip.updates[-1] is False
    finally:
        hub.close()


def test_failed_subscription_is_latched_without_a_frame_or_retry_storm():
    window, strip = Window(), Strip()
    attempts = []
    hub = WindowSpectrograms(window)

    def fail(_columns):
        attempts.append(True)
        raise OSError("Synthetic missing broker")

    try:
        hub.bind(strip, "one", fail)
        hub.bind(strip, "one", fail)
        assert len(attempts) == 1 and not window.ticks
        assert hub.sessions["one"].ended
    finally:
        hub.close()


def test_saving_phase_releases_feed_and_keeps_the_last_mapped_picture_static():
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    try:
        hub.bind(strip, "one", feeds)
        window.frame(100)
        model = strip.model
        hub.bind(strip, "one", feeds, active=False)
        assert not window.ticks and not hub.sessions and feeds.created[0].closes == 1
        assert strip.model is model
        strip.mapped = False
        strip.emit("unmap")
        assert strip.model is None
    finally:
        hub.close()


def test_stopping_freezes_last_model_unsubscribes_and_never_reopens():
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    hub.bind(strip, "one", feeds)
    window.frame(100)
    model = strip.model
    hub.bind(strip, "one", feeds, stopping=True)
    assert strip.stopping and strip.model is model
    assert strip.updates[-1] is False and not window.ticks
    assert feeds.created[0].closes == 1
    hub.bind(strip, "one", feeds, stopping=True)
    assert len(feeds.created) == 1 and strip.model is model
    hub.remove(strip)
    hub.close()


def test_first_observation_while_saving_never_opens_a_capture_feed():
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    hub.bind(strip, "one", feeds, stopping=True)
    assert not feeds.created and not window.ticks and strip.stopping
    hub.close()


def test_theme_and_motion_changes_reconfigure_mapped_views_without_new_subscriptions():
    window, feeds, strip = Window(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    hub.bind(strip, "one", feeds)
    hub.configure(dark=True, reduced_motion=True)
    assert strip.style == dict(dark=True, reduced_motion=True)
    assert len(feeds.created) == 1 and len(window.ticks) == 1
    hub.close()


def test_unchanged_refresh_keeps_current_feed_but_remap_uses_latest_factory():
    window, old, new, strip = Window(), Feeds(), Feeds(), Strip()
    hub = WindowSpectrograms(window)
    hub.bind(strip, "one", old)
    hub.bind(strip, "one", new)
    assert len(old.created) == 1 and not new.created
    strip.mapped = False
    strip.emit("unmap")
    assert old.created[0].closes == 1
    strip.mapped = True
    strip.emit("map")
    assert len(new.created) == 1 and len(window.ticks) == 1
    hub.close()
