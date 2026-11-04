# SPDX-License-Identifier: GPL-3.0-or-later
"""R79 §1 prerequisite: each native side opens on its own at start.

The devices are scripted appsrc branches on a fake clock: no sound server,
and no wall-time wait stands in for a device event.
"""

import json
import logging
import struct
import threading
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("GStreamer typelib is unavailable", allow_module_level=True)
from gi.repository import GLib, Gst  # noqa: E402
from test_recorder import spin_until  # noqa: E402

from cassini_talk.native_recorder import START_FAILURE, NativeRecorder  # noqa: E402

Gst.init(None)
FRAME = bytes(960)  # 10 ms of mono S16LE at 48 kHz.
FRAME_NS = 10 * Gst.MSECOND
BOUND = 3  # R79 §1: the microphone has opened when its first buffer arrives within 3 s.
CAPS = "audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved"


class RefusingBin(Gst.Bin):
    """A device whose state change fails without posting a bus error."""

    __gtype_name__ = "CassiniTestRefusingBin"

    def do_change_state(self, transition):
        if transition == Gst.StateChange.READY_TO_PAUSED:
            return Gst.StateChangeReturn.FAILURE
        return Gst.Bin.do_change_state(self, transition)


class BlockingBin(Gst.Bin):
    """A device whose state change blocks until the test releases it.

    Like a Pulse source that cannot reach its server: its state lock is held
    the whole time, so anything that changes its state waits too.
    """

    __gtype_name__ = "CassiniTestBlockingBin"

    def do_change_state(self, transition):
        if transition == Gst.StateChange.NULL_TO_READY:
            self.entered.set()
            self.release.wait(60)  # Bound only: the test releases it.
        return Gst.Bin.do_change_state(self, transition)


class Sides:
    """Scripted native sides on a fake open clock.

    plan maps a kind to (behaviour, seconds after its branch was built):
    "ok" admits a first buffer then, "error" posts a bus error and ends its
    stream then, "fail" fails its state change, "never" stays silent, and
    "block" holds its state change until release is set. The recorder's
    bounded open wait first waits for a "block" side's state change to begin
    and every other side's to return, then advances the fake clock to the next scripted event, fires
    it, and waits for the recorder to signal that it saw it. causes names an
    "error" side's cause, by kind. With frozen set, the clock never advances
    without a scripted event; overshoot lets the wait wake that much late;
    stop_on_commit names a side whose commit the user follows with a Stop.
    feed() continues a side's PCM right after its last buffer, as a device
    that kept delivering does: the fake open clock never lets the real time
    a loaded test spent in between become a recorded gap.
    """

    def __init__(self, recorder, plan, causes=None, codes=None):
        self.recorder, self.plan, self.causes = recorder, dict(plan), dict(causes or {})
        # An "error" side's GStreamer resource error code, by kind (OPEN_READ by default).
        self.codes = dict(codes or {})
        self.now = 0.0
        self.built, self.sources, self.fired = {}, {}, set()
        self.started_at = None
        self.frozen, self.overshoot, self.stop_on_commit = False, 0.0, None
        self.release, self.blocked = threading.Event(), {}
        self.next_pts = {}  # kind -> where its device's next buffer starts
        self.real_branch = recorder._branch
        recorder._open_clock = lambda: self.now
        recorder._open_wait = self.wait
        recorder._branch = self.branch
        recorder.connect("started", lambda *_: setattr(self, "started_at", self.now))

    def branch(self, kind):
        if kind not in self.plan:
            return self.real_branch(kind)  # A later turn-on uses the test source.
        self.built[kind] = self.now
        if self.plan[kind][0] == "fail":
            return RefusingBin()
        if self.plan[kind][0] == "block":
            branch = BlockingBin()
            branch.entered, branch.release = threading.Event(), self.release
            self.blocked[kind] = branch
            return branch
        branch = Gst.parse_bin_from_description(
            f'appsrc name=device is-live=true format=time do-timestamp=true caps="{CAPS}" ! '
            f"appsink name={kind} emit-signals=true sync=false async=false max-buffers=8 drop=false",
            False,
        )
        branch.get_by_name(kind).connect("new-sample", self.recorder._sample, kind)
        self.sources[kind] = branch.get_by_name("device")
        self.next_pts.pop(kind, None)
        branch.get_by_name("device").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._sent, kind)
        return branch

    def _sent(self, _pad, info, kind):
        buffer = info.get_buffer()
        if buffer.pts != Gst.CLOCK_TIME_NONE:
            self.next_pts[kind] = max(self.next_pts.get(kind, 0), buffer.pts + FRAME_NS)
        return Gst.PadProbeReturn.OK

    def fire(self, kind):
        self.fired.add(kind)
        source = self.sources[kind]
        if self.plan[kind][0] == "error":
            # A failing source reports its cause, then a stream error, then ends.
            code = int(self.codes.get(kind, Gst.ResourceError.OPEN_READ))
            for problem in (self.causes.get(kind, "Fake device went away"), "Internal data stream error."):
                error = GLib.Error.new_literal(Gst.ResourceError.quark(), problem, code)
                source.post_message(Gst.Message.new_error(source, error, "scripted"))
            return source.emit("end-of-stream")
        return source.emit("push-buffer", Gst.Buffer.new_wrapped(FRAME))

    def feed(self, kind, count):
        # Only after a side committed on its first buffer: that buffer has
        # passed the source pad, so its stamp is known here.
        assert kind in self.next_pts, f"{kind} has delivered no buffer yet"
        start = self.next_pts[kind]
        self.next_pts[kind] = start + count * FRAME_NS
        for index in range(count):
            buffer = Gst.Buffer.new_wrapped(FRAME)
            buffer.pts, buffer.duration = start + index * FRAME_NS, FRAME_NS
            assert self.sources[kind].emit("push-buffer", buffer) == Gst.FlowReturn.OK

    def _inflight(self):
        # Under the recorder's lock: a side marks itself settled and sets the
        # open wake in one hold, so a settled side's wake is already set.
        with self.recorder._lock:
            pending = dict(self.recorder._pending_sources)
            changing = any(
                not attempt.get("settled", True) and self.plan.get(kind, ("",))[0] != "block"
                for kind, attempt in pending.items()
            )
            return changing or any(kind in pending and not pending[kind]["ready"].is_set() for kind in self.fired)

    def wait(self, event, timeout):
        for branch in self.blocked.values():
            # A stuck side is stuck from the start: its state change begins
            # before any time passes, however late its thread is scheduled.
            branch.entered.wait(5)  # Bound only: its state-change thread enters it.
        if self._inflight():
            return event.wait(5)  # Bound only: the recorder signals admission.
        if event.is_set():
            return True  # Like a real wait: no time passes for an event already set.
        scripted = {
            kind: self.built[kind] + delay
            for kind, (behaviour, delay) in self.plan.items()
            if kind in self.built and kind not in self.fired and behaviour in ("ok", "error")
        }
        due = min((at for at in scripted.values() if at <= self.now + timeout + self.overshoot), default=None)
        if due is None:
            if self.frozen:
                return event.wait(5)  # Bound only: a Stop or a state change wakes it.
            self.now += timeout
            return event.is_set()
        self.now = due
        fired = [kind for kind, at in scripted.items() if at <= due]
        for kind in fired:
            self.fire(kind)
        seen = event.wait(5)
        if self.stop_on_commit in fired:
            assert self.stop_on_commit in self.recorder._track_ids  # It committed on its first buffer.
            self.recorder.stop()  # The user stops before the start's next pass.
        return seen


def start(tmp_path, plan, causes=None):
    recorder = NativeRecorder()
    sides = Sides(recorder, plan, causes)
    signals = []
    recorder.connect("started", lambda _r, path: signals.append(("started", path)))
    recorder.connect("error", lambda _r, message: signals.append(("error", message)))
    recorder.connect("finished", lambda _r, path: signals.append(("finished", path)))
    recorder.start(tmp_path, test=True)
    spin_until(lambda: not recorder.starting)
    return recorder, sides, signals


def settle():
    """Run everything already queued on the GTK main context."""
    done = []
    GLib.idle_add(lambda: done.append(True) and False, priority=GLib.PRIORITY_LOW)
    spin_until(lambda: bool(done))


def close(recorder):
    recorder.stop()
    spin_until(lambda: not recorder.starting and recorder.pipeline is None and not recorder._finishing)
    recorder._executor.shutdown(wait=True)


def kinds_of(session):
    return [track["kind"] for track in session.manifest["tracks"]]


def start_selection(session):
    return [e for e in session.manifest["events"] if e["type"] in ("selection", "othersSource")]


def refusals(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "did not open" in r.getMessage()]


OTHER = {"microphone": "system", "system": "microphone"}
SIDE = {"microphone": "me", "system": "others"}
REASONS = {
    "fail": "Could not start native audio capture",
    "error": "Fake device went away",
    "never": "Native audio capture did not produce a sample",
}


@pytest.mark.parametrize(
    ("behaviour", "refused"),
    # D58.1: a silent system side is never refused (test_others_armed.py).
    [(b, r) for b in ("fail", "error", "never") for r in ("microphone", "system") if (b, r) != ("never", "system")],
)
def test_one_refused_side_gets_no_track_while_the_other_records(tmp_path, caplog, refused, behaviour):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    opened = OTHER[refused]
    # A failed state change is refused at once, not at the shared deadline.
    recorder, sides, signals = start(tmp_path, {refused: (behaviour, 0.5), opened: ("ok", 0.1)})
    try:
        session = recorder.session
        assert [name for name, _ in signals] == ["started"], signals
        assert sides.started_at == (0.5 if behaviour == "error" else BOUND if behaviour == "never" else 0.1)
        assert kinds_of(session) == [opened]
        assert set(session.writers) == {opened} and set(recorder._track_ids) == {opened}
        assert not (session.directory / f"track-{refused}.wav").exists()
        assert (session.directory / f"track-{opened}.wav").exists()
        assert not recorder._pending_sources and refused not in recorder._branches
        # The start selection still records what was requested.
        assert start_selection(session) == [
            dict(atMs=0, type="othersSource", source="system", reason="missing"),
            dict(atMs=0, type="selection", me=True, others=True, reason="start"),
        ]
        assert refusals(caplog) == [
            f"Native {SIDE[refused]} side ({refused}) did not open at start: {REASONS[behaviour]}"
        ]
        # D58: the stored message is raw: a GStreamer error's domain, code and debug string.
        raw = {
            "error": f"gst-resource-error-quark:{int(Gst.ResourceError.OPEN_READ)}: scripted",
            "fail": "Could not start native audio capture",
            "never": "Native audio capture did not produce a sample",
        }
        assert session.manifest["refusals"] == [
            dict(atMs=0, side=SIDE[refused], code="deviceUnavailable", message=raw[behaviour])
        ]
        sides.feed(opened, 10)
        spin_until(lambda: session.writers[opened].frames >= 4800)
        assert recorder.levels[0 if refused == "microphone" else 1] == 0
        assert session.manifest["state"] == "recording" and not recorder.stopping
    finally:
        close(recorder)
    assert session.manifest["state"] == "completed"
    assert [name for name, _ in signals] == ["started", "finished"]
    saved = json.loads((session.directory / "session.json").read_text())
    assert [track["kind"] for track in saved["tracks"]] == [opened]


def test_both_sides_open_and_record(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.2), "system": ("ok", 0.3)})
    try:
        session = recorder.session
        assert [name for name, _ in signals] == ["started"] and sides.started_at == 0.3
        assert sorted(kinds_of(session)) == ["microphone", "system"]
        assert {t["file"] for t in session.manifest["tracks"]} == {"track-microphone.wav", "track-system.wav"}
        assert not refusals(caplog)
        # One checkpoint holds each opened track with its start.
        saved = json.loads((session.directory / "session.json").read_text())
        assert sorted(t["startOffsetMs"] for t in saved["tracks"]) == sorted(
            t["startOffsetMs"] for t in session.manifest["tracks"]
        )
        assert all(t["startOffsetMs"] >= 0 for t in saved["tracks"])
        for kind in ("microphone", "system"):
            sides.feed(kind, 10)
        spin_until(lambda: all(writer.frames >= 4800 for writer in session.writers.values()))
    finally:
        close(recorder)
    assert session.manifest["state"] == "completed"


CAUSES = {"microphone": "Fake microphone is held by another application", "system": "Fake monitor went away"}


@pytest.mark.parametrize(
    ("plan", "problem"),
    [
        # Both report a cause: the microphone's, though the monitor's came first.
        ({"microphone": ("error", 0.4), "system": ("error", 0.2)}, CAUSES["microphone"]),
        # A failed state change reports no cause; the other side's is kept.
        ({"microphone": ("fail", 0), "system": ("error", 0.4)}, CAUSES["system"]),
        # No side reports a cause: today's generic start failure.
        ({"microphone": ("fail", 0), "system": ("fail", 0)}, START_FAILURE),
    ],
)
def test_both_sides_refused_leaves_no_folder_and_shows_the_cause(tmp_path, caplog, plan, problem):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, _sides, signals = start(tmp_path, plan, CAUSES)
    try:
        spin_until(lambda: any(name == "finished" for name, _ in signals))
        # D58 point 2: nothing opened, so there is no recording and no folder.
        # The error still names the device's cause; the banner lists the refusals.
        assert signals == [("error", problem), ("finished", "")]
        assert recorder.discarded and not recorder.session.directory.exists()
        assert not list(tmp_path.iterdir())
        assert [(r["side"], r["code"]) for r in recorder.refusals] == [
            ("me", "deviceUnavailable"),
            ("others", "deviceUnavailable"),
        ]
        assert len(refusals(caplog)) == 2
        assert recorder.pipeline is None and not recorder.starting
    finally:
        close(recorder)


def test_a_first_buffer_after_the_bound_is_refused_and_never_becomes_a_track(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", BOUND + 0.5), "system": ("ok", 0.1)})
    try:
        session = recorder.session
        assert [name for name, _ in signals] == ["started"] and sides.started_at == BOUND
        assert "microphone" not in sides.fired
        # The device delivers late: its branch is already released.
        assert sides.fire("microphone") != Gst.FlowReturn.OK
        settle()
        assert kinds_of(session) == ["system"] and set(recorder._track_ids) == {"system"}
        assert not (session.directory / "track-microphone.wav").exists()
        assert refusals(caplog) == ["Native me side (microphone) did not open at start: " + REASONS["never"]]
    finally:
        close(recorder)


@pytest.mark.parametrize(("delay", "opens"), [(BOUND - 0.001, True), (BOUND, False), (BOUND + 0.1, False)])
def test_a_first_buffer_is_judged_by_its_admission_time_against_the_bound(tmp_path, caplog, delay, opens):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder = NativeRecorder()
    sides = Sides(recorder, {"microphone": ("ok", delay), "system": ("ok", 0.1)})
    # The open wait wakes late: the microphone's first buffer is admitted at
    # its scripted time even when that is past the deadline. R79 §1: a side
    # opens only when its first buffer arrives within 3 s, the bound excluded.
    sides.overshoot = 0.2
    recorder.start(tmp_path, test=True)
    spin_until(lambda: not recorder.starting)
    try:
        session = recorder.session
        assert "microphone" in sides.fired and sides.now == pytest.approx(delay)
        if opens:
            assert kinds_of(session) == ["microphone", "system"] and not refusals(caplog)
            spin_until(lambda: session.writers["microphone"].frames == 480)  # Its first buffer.
        else:
            assert kinds_of(session) == ["system"] and set(recorder._track_ids) == {"system"}
            assert not (session.directory / "track-microphone.wav").exists()
            assert refusals(caplog) == ["Native me side (microphone) did not open at start: " + REASONS["never"]]
    finally:
        close(recorder)


@pytest.mark.parametrize("stop", ["opening", "started"])
@pytest.mark.parametrize("blocked", ["microphone", "system"])
def test_a_side_stuck_in_its_state_change_blocks_neither_the_other_side_nor_stop(tmp_path, caplog, blocked, stop):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    healthy = OTHER[blocked]
    recorder = NativeRecorder()
    sides = Sides(recorder, {blocked: ("block", 0), healthy: ("ok", 0.1)})
    # "opening": the clock never passes the deadline, so the user stops while
    # the start is still opening. "started": the stuck side's own deadline,
    # taken before its state change, refuses it at the bound.
    sides.frozen = stop == "opening"
    signals = []
    recorder.connect("started", lambda _r, path: signals.append("started"))
    recorder.connect("finished", lambda _r, path: signals.append("finished"))
    recorder.start(tmp_path, test=True)
    try:
        spin_until(lambda: blocked in sides.blocked and sides.blocked[blocked].entered.is_set())
        # The healthy side commits at its first buffer and records it, while
        # the other side's state change still holds its state lock.
        spin_until(lambda: healthy in recorder._track_ids)
        session = recorder.session
        spin_until(lambda: session.writers[healthy].frames == 480)
        if stop == "opening":
            assert recorder.starting and not refusals(caplog)
        else:
            spin_until(lambda: signals == ["started"])
            assert sides.now == BOUND and sides.started_at == BOUND
            # D58.1: a stuck system side stays armed, unrefused; the microphone is refused.
            assert refusals(caplog) == (
                [f"Native {SIDE[blocked]} side ({blocked}) did not open at start: {REASONS['never']}"]
                if blocked == "microphone"
                else []
            )
        assert not sides.release.is_set()
        sides.feed(healthy, 9)
        spin_until(lambda: session.writers[healthy].frames == 4800)
        # Stop does not wait on the stuck state change either.
        recorder.stop()
        spin_until(lambda: "finished" in signals and recorder.pipeline is None and not recorder._finishing, timeout=5)
        assert not sides.release.is_set()
        assert signals == ["started", "finished"]
        assert session.manifest["state"] == "completed"
        saved = json.loads((session.directory / "session.json").read_text())
        assert [t["kind"] for t in saved["tracks"]] == [healthy]
        assert not (session.directory / f"track-{blocked}.wav").exists()
    finally:
        sides.release.set()
        branch = sides.blocked.get(blocked)
        # Released, the stuck side's own thread stops its detached branch.
        spin_until(lambda: not any(t.name == f"cassini-open-{blocked}" for t in threading.enumerate()))
        close(recorder)
    assert branch.get_parent() is None and branch.get_state(0)[1] == Gst.State.NULL


def test_a_side_that_commits_just_before_a_stop_is_reported_as_opened(tmp_path):
    recorder = NativeRecorder()
    sides = Sides(recorder, {"microphone": ("ok", 0.1), "system": ("block", 0)})
    sides.frozen, sides.stop_on_commit = True, "microphone"
    signals = []
    recorder.connect("started", lambda *_: signals.append("started"))
    recorder.connect("finished", lambda *_: signals.append("finished"))
    recorder.start(tmp_path, test=True)
    try:
        spin_until(lambda: "finished" in signals)
        # The start saw the Stop before it saw the commit: the side still opened.
        assert signals == ["started", "finished"]
        saved = json.loads((recorder.session.directory / "session.json").read_text())
        assert [t["kind"] for t in saved["tracks"]] == ["microphone"] and saved["state"] == "completed"
    finally:
        sides.release.set()
        spin_until(lambda: not any(t.name == "cassini-open-system" for t in threading.enumerate()))
        close(recorder)


def test_an_opening_side_keeps_the_error_that_names_its_cause():
    recorder = NativeRecorder()
    branch, source = Gst.Bin.new(None), Gst.ElementFactory.make("appsrc", None)
    branch.add(source)
    attempt = dict(branch=branch, ready=threading.Event(), error=None, wake=threading.Event())
    recorder._pending_sources["microphone"] = attempt
    try:
        # A failed source reports its cause, then the stream error that follows.
        for problem in ("Fake device went away", "Internal data stream error."):
            recorder._message(None, Gst.Message.new_error(source, GLib.Error(problem), "scripted"))
        assert attempt["error"] == "Fake device went away"
        assert attempt["ready"].is_set() and attempt["wake"].is_set()
        assert not recorder._finishing
    finally:
        recorder._executor.shutdown(wait=True)


def test_the_microphone_track_stays_first_when_the_system_opens_first(tmp_path):
    recorder, _sides, _signals = start(tmp_path, {"microphone": ("ok", 0.6), "system": ("ok", 0.1)})
    try:
        session = recorder.session
        # Speaker colours and speech order follow this order, not the race.
        assert kinds_of(session) == ["microphone", "system"]
        saved = json.loads((session.directory / "session.json").read_text())
        assert [t["kind"] for t in saved["tracks"]] == ["microphone", "system"]
        assert [t["file"] for t in saved["tracks"]] == ["track-microphone.wav", "track-system.wav"]
    finally:
        close(recorder)


def test_a_selection_of_only_a_refused_side_reopens_it_or_is_rejected(tmp_path):
    recorder, _sides, _signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("ok", 0.1)})
    try:
        session = recorder.session
        events = list(session.manifest["events"])
        # Others off would leave only the refused microphone: it is opened
        # again as a turn-on, still refuses, and the change is rejected.
        future = recorder.select(True, False)
        spin_until(lambda: future.done() and not recorder._changing_sources)
        with pytest.raises(OSError):
            future.result()
        assert session.manifest["events"] == events and kinds_of(session) == ["system"]
        assert set(recorder._track_ids) == {"system"} and session.manifest["state"] == "recording"
    finally:
        close(recorder)


def test_a_stop_before_any_side_opened_leaves_no_folder(tmp_path):
    recorder = NativeRecorder()
    Sides(recorder, {"microphone": ("never", 0), "system": ("never", 0)})
    recorder._open_wait = lambda event, timeout: event.wait(5)  # Only the stop wakes it.
    signals = []
    for name in ("started", "error", "finished"):
        recorder.connect(name, lambda _r, value, name=name: signals.append((name, value)))
    recorder.start(tmp_path, test=True)
    try:
        spin_until(lambda: len(recorder._pending_sources) == 2)
        recorder.stop()
        spin_until(lambda: any(name == "finished" for name, _ in signals))
        # The user cancelled this start: no recording, no folder, no banner.
        assert signals == [("finished", "")] and recorder.discarded
        assert not list(tmp_path.iterdir()) and recorder.refusals == []
    finally:
        close(recorder)


def test_slow_sides_open_in_parallel_within_one_bound(tmp_path):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 2.5), "system": ("ok", 2.5)})
    try:
        # Both sides take 2.5 s: the start waits 2.5 s, not 5 s.
        assert [name for name, _ in signals] == ["started"]
        assert sides.started_at == pytest.approx(2.5)
        assert sides.built == {"microphone": 0.0, "system": 0.0}
        assert sorted(kinds_of(recorder.session)) == ["microphone", "system"]
    finally:
        close(recorder)


@pytest.mark.parametrize("refused", ["microphone", "system"])
def test_a_refused_side_can_be_turned_off_and_on_again(tmp_path, refused):
    from cassini_talk.session_events import source_state

    opened = OTHER[refused]
    recorder, sides, _signals = start(tmp_path, {refused: ("fail", 0), opened: ("ok", 0.1)})

    def select(me, others):
        future = recorder.select(me, others)
        spin_until(lambda: future.done() and not recorder._changing_sources)
        return future.result()

    try:
        session = recorder.session
        me, others = refused != "microphone", refused != "system"
        state = select(me, others)
        assert (state.me, state.others) == (me, others)
        last = session.manifest["events"][-2 if refused == "system" else -1 :]
        assert last[0] == dict(atMs=last[0]["atMs"], type="selection", me=me, others=others, reason="user")
        if refused == "system":
            assert last[1] == dict(atMs=last[0]["atMs"], type="othersSource", source="none", reason="user")
        assert kinds_of(session) == [opened]
        # Turning it on again is an ordinary turn-on of the side, here on a
        # device that now works.
        sides.plan.pop(refused)
        state = select(True, True)
        assert (state.me, state.others) == (True, True)
        assert sorted(kinds_of(session)) == ["microphone", "system"]
        assert source_state(session.manifest["events"]).source == "system"
    finally:
        close(recorder)


# The browser path's native taps (BrowserCapture.start).


class Taps:
    """A tap factory whose opens the test scripts; no device."""

    def __init__(self, refuse=(), barrier=None, gate=()):
        self.refuse, self.barrier, self.gate = dict(refuse), barrier, dict(gate)
        self.made = []

    def __call__(self, kind, origin, append, failed, *, level):
        taps = self

        class Tap:
            pipeline = None

            def __init__(self):
                self.kind, self.append, self.failed, self.level = kind, append, failed, level
                self.started = self.stopped = False

            def start(self):
                if taps.barrier is not None:
                    taps.barrier.wait()  # Every side must be opening at once.
                if kind in taps.gate:
                    assert taps.gate[kind].wait(5)  # Bound only: opens after its gate.
                if taps.refuse.get(kind):
                    taps.refuse[kind] -= 1
                    raise OSError(f"Fake {kind} refused")
                self.started = True
                self.append(struct.pack("<hh", 1, 2), 0)  # Readiness PCM before commit.

            def stop(self):
                self.stopped = True

        tap = Tap()
        self.made.append(tap)
        return tap

    def of(self, kind):
        return [tap for tap in self.made if tap.kind == kind]


def browser_start(tmp_path, taps, *, microphone=True, others_source="browser", prepare=None):
    from cassini_talk.browser_capture import BrowserCapture

    changes = []
    capture = BrowserCapture(lambda _capture, change: changes.append(change), parent=tmp_path, tap_factory=taps)
    if prepare is not None:
        prepare(capture)
    message = dict(type="start", captureId="sides", timelineOriginMs=int(time.time() * 1000), microphone=microphone)
    if others_source == "browser":
        result = capture.start(message)
    else:
        capture.browser_lost = True
        result = capture.start(message, others_source="system", reason="missing")
    return capture, result, changes


def native_pcm(capture, kind):
    """A committed side's device delivers 10 ms right at its span's start."""
    identifier, tap = capture.native_taps[kind]
    track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
    tap.append(struct.pack("<h", 7) * 480, track["startOffsetMs"])
    return track


def test_a_refused_microphone_tap_leaves_browser_audio_recording(tmp_path, caplog):
    from test_capture_record import declare, pcm

    caplog.set_level(logging.WARNING, logger="cassini_talk.browser_capture")
    taps = Taps(refuse={"microphone": 99})
    capture, result, changes = browser_start(tmp_path, taps)
    try:
        session = capture.host.session
        assert result["type"] == "started" and not capture.finished and not capture.error
        assert "error" not in changes
        assert session.manifest["tracks"] == [] and "microphone" not in capture.native_taps
        assert capture.microphone is None and not list(capture.directory.glob("track-*.wav"))
        assert start_selection(session) == [
            dict(atMs=0, type="othersSource", source="browser", reason="start"),
            dict(atMs=0, type="selection", me=True, others=True, reason="start"),
        ]
        assert refusals(caplog) == ["Native me side (microphone) did not open at start: Fake microphone refused"]
        assert capture.handle(declare("remote")) == dict(type="ack")
        assert capture.handle(pcm("remote", 4800, offset=capture.elapsed_ms())) == dict(type="ack")
        assert kinds_of(session) == ["remote"] and session.writers["remote"].frames == 4800
        assert capture.levels == [0.0, 0.0]
        # A browser loss falls back to computer sound without reopening the
        # refused microphone, and the recording goes on.
        capture.fallback()
        assert len(taps.of("microphone")) == 1 and set(capture.native_taps) == {"system"}
        assert native_pcm(capture, "system")["frames"] == 480
        assert not capture.finished and not capture.error
        # The browser source returns: the restore goes back to it, still
        # without reopening the refused microphone.
        at = session.manifest["events"][-1]["atMs"]
        capture.handle(pcm("remote", 480, sequence=1, offset=at + 100))
        capture.restore()
        assert not capture.browser_lost and not capture.native_taps
        assert len(taps.of("microphone")) == 1 and capture.refused_sides == {"microphone"}
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed"


def test_a_refused_microphone_tap_can_be_turned_off_and_on_again(tmp_path):
    taps = Taps(refuse={"microphone": 1})
    capture, _result, _changes = browser_start(tmp_path, taps)
    try:
        capture.select(False, True)
        assert capture.host.session.manifest["events"][-1]["me"] is False
        capture.select(True, True)
        assert len(taps.of("microphone")) == 2 and "microphone" in capture.native_taps
        assert native_pcm(capture, "microphone")["frames"] == 480
    finally:
        capture.stop()


def test_a_refused_system_tap_leaves_the_microphone_recording(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="cassini_talk.browser_capture")
    taps = Taps(refuse={"system": 1})
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        assert not capture.finished and "error" not in changes
        assert kinds_of(session) == ["microphone"] and set(capture.native_taps) == {"microphone"}
        assert refusals(caplog) == ["Native others side (system) did not open at start: Fake system refused"]
        # The discarded readiness PCM is not a recording span.
        assert session.writers[capture.native_taps["microphone"][0]].frames == 0
        assert native_pcm(capture, "microphone")["frames"] == 480
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed"


def test_browser_microphone_track_stays_first_when_the_system_tap_opens_first(tmp_path):
    system_added = threading.Event()

    def prepare(capture):
        add = capture._add_native

        def added(kind, *args, **kwargs):
            result = add(kind, *args, **kwargs)
            if kind == "system":
                system_added.set()
            return result

        capture._add_native = added

    taps = Taps(gate={"microphone": system_added})
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system", prepare=prepare)
    try:
        assert kinds_of(capture.host.session) == ["microphone", "system"]
        saved = json.loads((capture.directory / "session.json").read_text())
        assert [t["kind"] for t in saved["tracks"]] == ["microphone", "system"]
    finally:
        capture.stop()


@pytest.mark.parametrize("reopens", [False, True])
def test_turning_others_off_reopens_a_refused_microphone_or_is_rejected(tmp_path, reopens):
    from test_capture_record import declare, pcm

    # Repro A: the microphone was refused at start while browser audio records.
    taps = Taps(refuse={"microphone": 1 if reopens else 99})
    capture, _result, _changes = browser_start(tmp_path, taps)
    try:
        session = capture.host.session
        capture.handle(declare("remote"))
        capture.handle(pcm("remote", 480, offset=capture.elapsed_ms()))
        events = list(session.manifest["events"])
        if not reopens:
            # Recording nothing is never committed.
            with pytest.raises(OSError, match="Fake microphone refused"):
                capture.select(True, False)
            assert session.manifest["events"] == events and (capture.me, capture.others) == (True, True)
            assert not any(t.get("ended") for t in session.manifest["tracks"])
            assert capture.refused_sides == {"microphone"}
        else:
            capture.select(True, False)
            selection = [e for e in session.manifest["events"] if e["type"] == "selection"][-1]
            assert (selection["me"], selection["others"], selection["reason"]) == (True, False, "user")
            assert native_pcm(capture, "microphone")["frames"] == 480
            assert capture.refused_sides == set()
        assert len(taps.of("microphone")) == 2 and not capture.finished
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed"


@pytest.mark.parametrize("reopens", [False, True])
def test_turning_me_off_reopens_a_refused_stand_in_system_tap_or_is_rejected(tmp_path, reopens):
    # Repro B: the D49/StartRejected stand-in, its system tap refused.
    taps = Taps(refuse={"system": 1 if reopens else 99})
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        events = list(session.manifest["events"])
        if not reopens:
            with pytest.raises(OSError, match="Fake system refused"):
                capture.select(False, True)
            assert session.manifest["events"] == events and (capture.me, capture.others) == (True, True)
            assert kinds_of(session) == ["microphone"] and "microphone" in capture.native_taps
            assert native_pcm(capture, "microphone")["frames"] == 480
        else:
            capture.select(False, True)
            assert set(capture.native_taps) == {"system"}
            assert native_pcm(capture, "system")["frames"] == 480
            assert capture.refused_sides == set()
        assert len(taps.of("system")) == 2 and not capture.finished
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed"


def test_browser_native_taps_open_in_parallel(tmp_path):
    # Each tap's open waits until the other's has begun: a serial start
    # would break the barrier and refuse both.
    taps = Taps(barrier=threading.Barrier(2, timeout=5))
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    try:
        assert not capture.finished
        assert sorted(kinds_of(capture.host.session)) == ["microphone", "system"]
        assert all(tap.started for tap in taps.made)
    finally:
        capture.stop()


def test_every_refused_browser_native_tap_leaves_no_folder(tmp_path, caplog):
    from cassini_talk.refusals import NothingOpened

    caplog.set_level(logging.WARNING, logger="cassini_talk.browser_capture")
    taps = Taps(refuse={"microphone": 1, "system": 1})
    with pytest.raises(NothingOpened, match="Fake microphone refused") as raised:
        browser_start(tmp_path, taps, others_source="system")
    # D58 point 2: no folder; the start banner gets every refusal, in order.
    assert not list(tmp_path.iterdir())
    assert raised.value.refusals == [
        dict(atMs=0, side="me", code="deviceUnavailable", message="Fake microphone refused"),
        dict(atMs=0, side="others", code="deviceUnavailable", message="Fake system refused"),
    ]
    assert len(refusals(caplog)) == 2
