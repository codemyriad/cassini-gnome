# SPDX-License-Identifier: GPL-3.0-or-later
"""D61 one clock base (SPEC "Page trust and browser clocks", protocol #55).

A capture's timelineOriginMs is mapped onto CLOCK_MONOTONIC once; every host stamp of
the capture (selection, othersSource, refusal, resume ask, end, native PCM offsets) is
measured on that mapping, so a wall clock that steps or drifts during the capture moves
none of them. Only the wall clock is faked here (time.time/time.time_ns); the monotonic
clock is the real one, as the extension's and the protocol driver's are.
"""

import datetime
import struct
import sys
import threading
import time
import types

import pytest
from test_browser_resume_policy import peer, policy  # noqa: F401

from cassini_talk.browser_capture import BrowserCapture

# A plausible Unix time for monotonic 0, so the fake wall reads like a real one.
BASE_NS = 1_790_000_000 * 1_000_000_000
TOLERANCE_MS = 1  # Stamps are rounded to ms; the mapping's bracket is microseconds.


class Wall:
    """The wall clock: CLOCK_MONOTONIC + BASE_NS until go(), then stepped and drifting.

    Drift is per wall reading (a clock running fast), so no test waits for it.
    `reads` counts the app's wall readings after go().
    """

    def __init__(self):
        self.step_ns = self.drift_ns = 0
        self.reads = 0
        self.going = False

    def time_ns(self):
        return self._read(sys._getframe(1))

    def time(self):
        return self._read(sys._getframe(1)) / 1e9

    def _read(self, caller):
        if self.going:
            # Counted for the app's own reads (a log record's time is not a stamp).
            self.reads += caller.f_globals.get("__name__", "").startswith("cassini_talk")
            self.step_ns += self.drift_ns
        return time.monotonic_ns() + BASE_NS + self.step_ns

    def go(self, step_ms, drift_ms_per_read):
        self.going = True
        self.step_ns += step_ms * 1_000_000
        self.drift_ns = drift_ms_per_read * 1_000_000

    def step(self, step_ms):
        self.step_ns += step_ms * 1_000_000


@pytest.fixture
def wall(monkeypatch):
    """Fake every wall reading the app could take: time.time/time_ns, a CLOCK_REALTIME
    clock_gettime, datetime.now/utcnow/today and date.today in the loaded cassini_talk
    modules, and GLib.get_real_time. Each one steps, drifts and is counted."""
    fake = Wall()
    monkeypatch.setattr(time, "time_ns", fake.time_ns)
    monkeypatch.setattr(time, "time", fake.time)
    real_gettime, real_gettime_ns = time.clock_gettime, time.clock_gettime_ns

    def clock_gettime_ns(clock):
        return fake._read(sys._getframe(1)) if clock == time.CLOCK_REALTIME else real_gettime_ns(clock)

    def clock_gettime(clock):
        return fake._read(sys._getframe(1)) / 1e9 if clock == time.CLOCK_REALTIME else real_gettime(clock)

    monkeypatch.setattr(time, "clock_gettime_ns", clock_gettime_ns)
    monkeypatch.setattr(time, "clock_gettime", clock_gettime)

    # datetime's C clock reads are not time.time: stand-in classes read the fake instead.
    # Instances of the real classes still pass isinstance against a stand-in.
    class StandIn(type):
        def __instancecheck__(cls, instance):
            return isinstance(instance, cls.__mro__[1])

        def __subclasscheck__(cls, subclass):
            return issubclass(subclass, cls.__mro__[1])

    class Datetime(datetime.datetime, metaclass=StandIn):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime.fromtimestamp(fake._read(sys._getframe(1)) / 1e9, tz)

        @classmethod
        def utcnow(cls):
            utc = datetime.datetime.fromtimestamp(fake._read(sys._getframe(1)) / 1e9, datetime.UTC)
            return utc.replace(tzinfo=None)

        @classmethod
        def today(cls):
            return datetime.datetime.fromtimestamp(fake._read(sys._getframe(1)) / 1e9)

    class Date(datetime.date, metaclass=StandIn):
        @classmethod
        def today(cls):
            return datetime.date.fromtimestamp(fake._read(sys._getframe(1)) / 1e9)

    stand_ins = {datetime.datetime: Datetime, datetime.date: Date}
    as_module = types.SimpleNamespace(**{**vars(datetime), "datetime": Datetime, "date": Date})
    for name, module in list(sys.modules.items()):
        if name == "cassini_talk" or name.startswith("cassini_talk."):
            for attribute, value in list(vars(module).items()):
                if value is datetime:
                    monkeypatch.setattr(module, attribute, as_module)
                elif isinstance(value, type) and value in stand_ins:
                    monkeypatch.setattr(module, attribute, stand_ins[value])
    try:
        from gi.repository import GLib
    except (ImportError, ValueError):
        pass
    else:
        monkeypatch.setattr(GLib, "get_real_time", lambda: fake._read(sys._getframe(1)) // 1000)
    return fake


def expected(origin_ms, monotonic_ns):
    """The session time of a monotonic reading on the origin's mapping before any step."""
    return (monotonic_ns + BASE_NS) / 1e6 - origin_ms


def stamped(origin_ms, action):
    """Run `action`; return its result and the session-time bounds around it."""
    before = time.monotonic_ns()
    result = action()
    after = time.monotonic_ns()
    return result, expected(origin_ms, before) - TOLERANCE_MS, expected(origin_ms, after) + TOLERANCE_MS


class Taps:
    """Scripted native taps; the next open of a kind in `refuse` is refused."""

    def __init__(self):
        self.refuse = set()

    def __call__(self, kind, _clock, append, failed, *, level):
        taps = self

        class Tap:
            pipeline = None

            def start(self):
                if kind in taps.refuse:
                    taps.refuse.discard(kind)
                    raise OSError(f"Fake {kind} refused")
                append(struct.pack("<hh", 1, 2), 0)  # Readiness PCM before commit.

            def stop(self):
                pass

        return Tap()


def last(session, **match):
    return [e for e in session.manifest["events"] if all(e.get(k) == v for k, v in match.items())][-1]


def test_the_fake_wall_steps_and_counts_every_wall_reading_of_the_app(wall):
    """The guard behind `wall.reads == 0`: a wall reading the app takes by any of these
    routes is stepped with the fake and counted, so a reintroduced one fails the tests."""
    from cassini_talk import browser_capture, recording_session

    # Each reading is evaluated in a cassini_talk module's namespace, as the app's own is.
    sources = {
        "time.time": "time.time() * 1e9",
        "time.time_ns": "time.time_ns()",
        "clock_gettime_ns": "time.clock_gettime_ns(time.CLOCK_REALTIME)",
        "clock_gettime": "time.clock_gettime(time.CLOCK_REALTIME) * 1e9",
        "datetime.now": "recording_session.datetime.now(UTC).timestamp() * 1e9",
        "datetime.utcnow": "browser_capture.datetime.utcnow().replace(tzinfo=UTC).timestamp() * 1e9",
    }
    app = dict(
        __name__="cassini_talk.probe",
        time=time,
        UTC=datetime.UTC,
        browser_capture=browser_capture,
        recording_session=recording_session,
    )
    try:
        from gi.repository import GLib
    except (ImportError, ValueError):
        pass
    else:
        sources["GLib.get_real_time"] = "GLib.get_real_time() * 1000"
        app["GLib"] = GLib
    wall.go(2000, 0)
    for count, (name, source) in enumerate(sources.items(), 1):
        before = time.monotonic_ns()
        read = eval(source, app)
        after = time.monotonic_ns()
        assert before + BASE_NS + 2e9 - 1e6 <= read <= after + BASE_NS + 2e9 + 1e6, name
        assert wall.reads == count, f"{name} is not counted"
    assert time.clock_gettime_ns(time.CLOCK_MONOTONIC) > 0 and wall.reads == len(sources)


def test_the_mapping_reads_the_wall_clock_once_and_never_again(wall):
    from cassini_talk.session_clock import SessionClock

    origin = time.time_ns() // 1_000_000 - 1500  # The page started 1.5 s ago.
    wall.go(0, 0)
    clock = SessionClock(origin)
    assert wall.reads == 1
    wall.step(2000)
    wall.drift_ns = 250_000_000
    before = time.monotonic_ns()
    now = clock.now()
    after = time.monotonic_ns()
    assert expected(origin, before) - TOLERANCE_MS <= now <= expected(origin, after) + TOLERANCE_MS
    assert now >= 1500 - TOLERANCE_MS
    assert clock.at(before) == pytest.approx(expected(origin, before), abs=TOLERANCE_MS)
    assert wall.reads == 1, "reading the session clock never reads the wall clock"


def test_an_origin_gnome_picks_is_mapped_from_the_same_wall_reading(wall):
    from cassini_talk.session_clock import SessionClock

    wall.go(0, 0)
    before = time.monotonic_ns()
    clock = SessionClock()
    after = time.monotonic_ns()
    assert wall.reads == 1
    assert expected(clock.origin_ms, before) - TOLERANCE_MS <= 0 <= expected(clock.origin_ms, after) + TOLERANCE_MS
    assert clock.at(before) <= TOLERANCE_MS and clock.at(after) >= -TOLERANCE_MS


def test_a_stand_in_origin_gnome_picks_is_mapped_from_its_own_reading(policy, wall):  # noqa: F811
    from test_refusal_retries import stand_in

    service, _tasks, _sent, parent = policy
    browser = peer(service)
    wall.go(0, 250)  # Every wall reading runs 250 ms fast.
    before = time.monotonic_ns()
    capture = stand_in(service, parent, browser)
    try:
        elapsed = capture.elapsed_ms()
        # The origin and its mapping are one reading: the session is as old as
        # the monotonic time since, however often the fast wall was read.
        assert 0 <= elapsed <= (time.monotonic_ns() - before) / 1e6 + TOLERANCE_MS
        assert capture.session_clock.origin_ms == capture.origin_ms
    finally:
        capture.stop()


def test_a_stepping_and_drifting_wall_clock_moves_no_capture_stamp(tmp_path, wall):
    from test_capture_record import declare, pcm

    taps = Taps()
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=taps)
    # The page set its origin 1.5 s before GNOME's start: start() maps that origin, not
    # GNOME's own "now".
    origin = int(time.time() * 1000) - 1500
    capture.start(
        dict(type="start", captureId="clock", timelineOriginMs=origin, microphone=True, callGeneration="g1"),
        others_source="system",
        reason="missing",
    )
    session = capture.host.session
    mapping = getattr(capture, "session_clock", None)
    # The capture runs on: the wall jumps 2 s ahead and runs 250 ms fast per reading.
    wall.go(2000, 250)
    try:
        _result, low, high = stamped(origin, lambda: capture.select(False, True))
        assert low <= last(session, type="selection", me=False)["atMs"] <= high
        assert low >= 1500 - TOLERANCE_MS

        wall.step(-4000)  # Now 2 s behind, still drifting.
        taps.refuse.add("microphone")
        before = time.monotonic_ns()
        with pytest.raises(OSError, match="Fake microphone refused"):
            capture.select(True, True)
        after = time.monotonic_ns()
        refusal = session.manifest["refusals"][-1]
        assert refusal["side"] == "me"
        assert expected(origin, before) - TOLERANCE_MS <= refusal["atMs"] <= expected(origin, after) + TOLERANCE_MS

        _result, low, high = stamped(origin, lambda: capture.mark_resume_ask("ask-1"))
        assert low <= capture.resume_asks["ask-1"] <= high

        reply = capture.resume(dict(captureId="clock", timelineOriginMs=origin, callGeneration="g1"))
        assert reply["resumed"] is True and reply["timelineOriginMs"] == origin
        assert capture.session_clock is mapping, "a resume keeps the capture's one mapping"

        capture.handle(declare("back"))
        offset = expected(origin, time.monotonic_ns())
        capture.handle(pcm("back", 480, offset=offset))
        _result, low, high = stamped(origin, capture.restore)
        restored = last(session, type="othersSource", source="browser")
        assert restored["reason"] == "callConnected" and low <= restored["atMs"] <= high + 10  # + the 10 ms PCM.

        _result, low, high = stamped(origin, capture.fallback)
        lost = last(session, type="othersSource", source="system", reason="lost")
        assert low <= lost["atMs"] <= high
        assert capture.session_clock is mapping, "a fallback keeps the capture's one mapping"
        assert wall.reads == 0, "no stamp of the running capture read the wall clock"

        _result, low, high = stamped(origin, capture.stop)
        assert low <= last(session, type="end")["atMs"] <= high
    finally:
        capture.stop()
    other = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Taps())
    other.start(dict(type="start", captureId="next", timelineOriginMs=int(time.time() * 1000), microphone=False))
    try:
        assert other.session_clock is not mapping, "a new capture maps its own origin"
    finally:
        other.stop()


def test_a_native_tap_opened_after_a_wall_step_stamps_pcm_on_the_monotonic_mapping(tmp_path, wall):
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("GStreamer typelib is unavailable")
    from cassini_talk.native_tap import NativeTap

    taps, heard = [], []

    def factory(kind, clock, append, failed, *, level):
        buffers, enough = [], threading.Event()

        def recording(data, offset, *, host_ns=None):
            buffers.append((offset, host_ns))
            if len(buffers) >= 5:
                enough.set()
            append(data, offset, host_ns=host_ns)

        taps.append(kind)
        heard.append((buffers, enough))
        return NativeTap(kind, clock, recording, failed, level=level, test=True)

    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=factory)
    origin = int(time.time() * 1000) - 1500  # The page's origin, 1.5 s before start.
    capture.start(dict(type="start", captureId="tap", timelineOriginMs=origin, microphone=True))
    try:
        assert taps == ["microphone"]
        wall.go(2000, 250)
        capture.select(False, True)
        capture.select(True, True)  # Its anchor is taken after the step.
        assert taps == ["microphone", "microphone"]
        # The tap opened after the step delivers PCM (bound only); the first one
        # delivered at least its opening buffer before it was turned off.
        assert heard[-1][1].wait(5), "the test source delivered no PCM"
        for buffers, _enough in heard:
            assert buffers
            for offset, host_ns in list(buffers):
                # GStreamer's SystemClock is CLOCK_MONOTONIC: base + pts is a monotonic reading.
                assert offset == pytest.approx(expected(origin, host_ns), abs=TOLERANCE_MS)
                assert offset >= 1500 - TOLERANCE_MS
        assert wall.reads == 0, "a tap reads no wall clock"
    finally:
        capture.stop()
