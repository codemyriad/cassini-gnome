#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""R93-2: a host stall delays native input without losing it, through real pulsesrc.

The shipped BrowserCapture with real NativeTaps (pulsesrc on the private Pulse's
null-sink monitors, so GstAudioBaseSrc's ring-buffer overrun is the real one):
Me on, the others' side on computer sound through a fallback, as at a call loss.
Each case blocks both taps' writers the way R93's runs were blocked, then saves:

  lock      another thread holds capture.lock (a slow checkpoint or browser
            packet) for 3.5 s, then 8 s
  fsync     one checkpoint fsync, under capture.lock, takes 2.5 s
  fsynceach every fsync of one checkpoint takes 0.7 s (R93's swapping host)
  stop      Stop is pressed on another thread 2 s into a capture.lock hold: the
            audio the queues hold from before it is saved, up to the Stop moment
            (the broker's paths mark Stop, then save)
  stopdirect the same with a direct capture.stop() (GNOME's own Stop, a
            policy Stop): it fixes Stop's moment before it waits for the lock
            the stall holds, which is held 2 s more
  stopbrowser the same with the browser's Stop message: it is admitted and its
            boundary published before it waits for the lock, so the tap
            callbacks queued behind the stall, which can take the lock before
            it, save nothing after it. In all three, the end event is at the
            Stop moment, however long the save took (D4)
  overrun   capture.lock is held until both 10 s admission queues are full,
            then 2 s more. Exact dropped callback frames become writer silence,
            interrupted/io and an error end, including losses shorter than 1 s.

A stall's length is audio time heard by an independent observer pulsesrc on the
same Pulse server, never wall time; each tap queue's level is observed on every
buffer entering it; a rejected callback marks the admission budget full. Every wait is on such
an event, with a bound only as an upper limit (E1). Before R93-2 each queue held
1 s: any of these stalls overran pulsesrc, its "Dropped N samples" warning
followed, and a drop of 1 s or more saved the meeting interrupted.
"""

import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
if not os.environ.get("PULSE_SERVER", "").startswith("unix:/tmp/"):
    raise SystemExit("A private Pulse socket is required")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from cassini_talk import recording_session  # noqa: E402
from cassini_talk.browser_capture import BrowserCapture  # noqa: E402
from cassini_talk.native_tap import NativeTap  # noqa: E402

Gst.init(None)
artifacts = Path(os.environ["CASSINI_TEST_ARTIFACTS"]) / "stall"
artifacts.mkdir(parents=True, exist_ok=True)
BOUND = 30  # seconds; an upper limit only, never waited out by a passing case
KINDS = ("microphone", "system")
drops = []

# Every native tap pipeline's bus, watched synchronously for GstAudioBaseSrc's
# "Dropped N samples" warning (the tap's own bus loop keeps only errors and EOS).
_parse_launch = Gst.parse_launch


def parse_launch(description):
    pipeline = _parse_launch(description)
    kind = "microphone" if "@DEFAULT_SOURCE@" in description else "system"

    def sync(_bus, message, *_):
        if message.type == Gst.MessageType.WARNING:
            found = re.search(r"Dropped (\d+) samples", message.parse_warning()[1] or "")
            if found:
                drops.append(dict(kind=kind, samples=int(found.group(1)), at=time.monotonic()))
        return Gst.BusSyncReply.PASS

    pipeline.get_bus().set_sync_handler(sync)
    return pipeline


Gst.parse_launch = parse_launch


def tap_queue(tap):
    """The tap's queue ahead of its writer: tap-queue, or the bare queue it replaced."""
    found = tap.pipeline.get_by_name("tap-queue")
    if found is not None:
        return found
    iterator = tap.pipeline.iterate_elements()
    while True:
        result, element = iterator.next()
        if result != Gst.IteratorResult.OK:
            raise AssertionError("The tap has no queue")
        if element.get_factory().get_name() == "queue":
            return element


class QueueLevels:
    """Each tap queue's level, observed on every buffer entering it."""

    def __init__(self, capture):
        self.condition = threading.Condition()
        self.levels, self.peaks, self.overruns, self.limits = {}, {}, set(), {}
        for kind in KINDS:
            tap = capture.native_taps[kind][1]
            admission = getattr(tap, "writer_queue", None)
            self.levels[kind] = self.peaks[kind] = 0.0
            if admission is not None:
                self.limits[kind] = admission.capacity / admission.rate
                admission.enter = self._observe(admission.enter, admission, kind)
            else:  # Historical comparison against the old C-queue-only writer.
                queue = tap_queue(tap)
                self.limits[kind] = queue.get_property("max-size-time") / Gst.SECOND
                queue.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._probe, kind, queue)
                queue.connect("overrun", self._overrun, kind)

    def _observe(self, enter, admission, kind):
        def observed(*args):
            accepted = enter(*args)
            state = admission.snapshot()
            with self.condition:
                level = state["buffered_frames"] / admission.rate
                self.levels[kind] = level
                self.peaks[kind] = max(self.peaks[kind], level)
                if state["dropped_frames"]:
                    self.overruns.add(kind)
                self.condition.notify_all()
            return accepted

        return observed

    def _probe(self, _pad, _info, kind, queue):
        level = queue.get_property("current-level-time") / Gst.SECOND
        with self.condition:
            self.levels[kind] = level
            self.peaks[kind] = max(self.peaks[kind], level)
            self.condition.notify_all()
        return Gst.PadProbeReturn.OK

    def _overrun(self, _queue, kind):
        with self.condition:
            self.overruns.add(kind)
            self.condition.notify_all()

    def full(self):
        with self.condition:
            assert self.condition.wait_for(lambda: set(KINDS) <= self.overruns, BOUND), self.snapshot()

    def snapshot(self):
        with self.condition:
            return dict(levels=dict(self.levels), peaks=dict(self.peaks), overruns=sorted(self.overruns))


class Observer:
    """An independent pulsesrc on the microphone's device: audio time that passes
    while the taps are stalled."""

    def __init__(self):
        self.pipeline = _parse_launch(
            'pulsesrc device=@DEFAULT_SOURCE@ client-name="Cassini stall observer" ! fakesink name=out sync=false'
        )
        self.condition, self.ns = threading.Condition(), 0
        self.pipeline.get_by_name("out").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._probe)
        assert self.pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE

    def _probe(self, _pad, info):
        with self.condition:
            duration = info.get_buffer().duration
            assert duration != Gst.CLOCK_TIME_NONE, "the observer's buffers carry no duration"
            self.ns += duration
            self.condition.notify_all()
        return Gst.PadProbeReturn.OK

    def after(self, seconds):
        with self.condition:
            target = self.ns + seconds * Gst.SECOND
            assert self.condition.wait_for(lambda: self.ns >= target, BOUND), "the observer heard no audio"

    def close(self):
        self.pipeline.set_state(Gst.State.NULL)


class Progress:
    """Wraps each tap's append: wakes waiters as its track's saved end advances."""

    def __init__(self, capture):
        self.capture, self.condition = capture, threading.Condition()
        for kind in KINDS:
            tap = capture.native_taps[kind][1]
            tap.append = self._wrap(tap.append)

    def _wrap(self, append):
        def appended(*args, **kwargs):
            try:
                return append(*args, **kwargs)
            finally:
                with self.condition:
                    self.condition.notify_all()

        return appended

    def ends(self):
        with self.capture.lock:
            session = self.capture.host.session
            return {
                t["kind"]: t["startOffsetMs"] + session.writers[t["id"]].frames * 1000 / 48000
                for t in session.manifest["tracks"]
                if t.get("startOffsetMs") is not None and not t.get("ended")
            }

    def reach(self, offset_ms):
        def done():
            ends = self.ends()
            return set(ends) == set(KINDS) and all(end >= offset_ms for end in ends.values())

        with self.condition:
            assert self.condition.wait_for(done, BOUND), f"tracks never reached {offset_ms} ms: {self.ends()}"


def hold_lock(capture, until):
    """Hold capture.lock on another thread until `until()` returns."""
    held = threading.Event()

    def hold():
        with capture.lock:
            held.set()
            until()

    thread = threading.Thread(target=hold, name="stall-injector")
    thread.start()
    assert held.wait(BOUND)
    return thread


def run_case(name, stall):
    del drops[:]
    directory = artifacts / name
    directory.mkdir()
    # A Library capture, so its native tracks keep clockAnchors.
    os.environ["CASSINI_LIBRARY_DIR"] = str(directory / "Library")
    capture = BrowserCapture(lambda *_: None, tap_factory=NativeTap)
    observer = Observer()
    fsync, record = os.fsync, dict(case=name)
    try:
        capture.start(dict(type="start", captureId=name, timelineOriginMs=int(time.time() * 1000), microphone=True))
        capture.fallback("lost")  # The others' side on computer sound.
        assert set(capture.native_taps) == set(KINDS), capture.native_taps
        levels, progress = QueueLevels(capture), Progress(capture)
        record["queueLimitS"] = levels.limits
        progress.reach(capture.elapsed_ms() + 500)  # Both sides record before the stall.
        record["stallStartMs"] = capture.elapsed_ms()
        record.update(stall(capture, levels, observer))
        record["stallEndMs"] = capture.elapsed_ms()
        record["queues"] = levels.snapshot()
        if not capture.stop_requested.is_set():
            progress.reach(record["stallEndMs"] + 500)  # The queued audio reached the WAVs.
        capture.stop()
        assert capture.stopped.wait(BOUND), "the capture was never published"
    finally:
        os.fsync = fsync
        observer.close()
        if not capture.stop_requested.is_set() or not capture.stopped.is_set():
            capture.stop()
    manifest = json.loads((capture.directory / "session.json").read_text())
    tracks = manifest["tracks"]
    record.update(
        drops=list(drops),
        state=manifest["state"],
        problemKind=manifest.get("problemKind"),
        problem=manifest.get("problem"),
        end=manifest["events"][-1],
        tracks=[
            dict(
                kind=t["kind"],
                startOffsetMs=t["startOffsetMs"],
                endOffsetMs=t.get("endOffsetMs"),
                frames=t["frames"],
                gapFrames=t.get("gapFrames"),
                droppedFrames=t.get("droppedFrames", 0),
                gaps=[g for g in t.get("gaps", []) if g["kind"] != "prefix"],
                anchors=t.get("clockAnchors"),
            )
            for t in tracks
        ],
    )
    return record


def assert_no_loss(record):
    assert not record["drops"], f"pulsesrc dropped input: {record['drops']}"
    assert record["state"] == "completed" and record["problemKind"] is None, record
    assert record["end"]["reason"] == "user", record["end"]
    assert sorted(t["kind"] for t in record["tracks"]) == sorted(KINDS)
    for track in record["tracks"]:
        assert not track["gaps"] and not track["gapFrames"], track
        # A drop under 1 s is absorbed: it leaves no gap, only host time the
        # frames do not cover. The anchors' host span must match the frames.
        (first_sample, first_ns), (last_sample, last_ns) = track["anchors"][0], track["anchors"][-1]
        frames_ms, host_ms = (last_sample - first_sample) / 48, (last_ns - first_ns) / 1e6
        assert abs(host_ms - frames_ms) <= 50, (track["kind"], host_ms, frames_ms)


def lock(seconds):
    def stall(capture, _levels, observer):
        hold_lock(capture, lambda: observer.after(seconds)).join(BOUND)
        return {}

    return stall


def slow_fsync(seconds, *, each=False):
    """One checkpoint's first fsync (or each of its fsyncs) takes `seconds` of
    audio, on whichever thread runs the checkpoint, under capture.lock."""

    def stall(_capture, _levels, observer):
        fsync, checkpoint = os.fsync, recording_session.RecordingSession.checkpoint
        state = dict(armed=True, slow=False, fsyncs=0)
        done = threading.Event()

        def slow(fd):
            if state["slow"] and threading.current_thread() is state["thread"]:
                observer.after(seconds)
                state["fsyncs"] += 1
                state["slow"] = each
            return fsync(fd)

        def timed(session):
            first = state["armed"]
            if first:
                state.update(armed=False, slow=True, thread=threading.current_thread())
            try:
                return checkpoint(session)
            finally:
                if first:
                    state["slow"] = False
                    done.set()

        os.fsync = slow  # run_case restores it.
        recording_session.RecordingSession.checkpoint = timed
        try:
            assert done.wait(BOUND), "no checkpoint ran"
        finally:
            recording_session.RecordingSession.checkpoint = checkpoint
        return dict(slowFsyncs=state["fsyncs"])

    return stall


def stop_during(seconds):
    """Stop is pressed mid-stall, as the broker's Stop paths mark it: request_stop
    on another thread, which needs no capture.lock (the stall still holds it);
    the save follows once the lock is free."""

    def stall(capture, _levels, observer):
        pressed = {}

        def press():
            pressed["ms"] = capture.elapsed_ms()
            getattr(capture, "request_stop", capture.stop_requested.set)()

        def until():
            observer.after(seconds)
            stop = threading.Thread(target=press, name="stall-stop")
            stop.start()
            stop.join(BOUND)  # Bound only: marking Stop never waits for capture.lock.
            pressed["blocked"] = stop.is_alive()

        hold_lock(capture, until).join(BOUND)
        assert pressed.get("blocked") is False, "marking Stop waited for capture.lock"
        return dict(stopAtMs=getattr(capture, "stop_at_ms", None) or pressed["ms"])

    return stall


def stop_direct_during(seconds, *, browser=False):
    """A direct capture.stop() (or the browser's Stop message) mid-stall: its Stop
    moment is fixed at once, while the stall goes on for `seconds` more; its save
    follows once the lock is free."""

    def stall(capture, _levels, observer):
        pressed, threads = {}, []
        press = capture.stop
        if browser:

            def press():
                return capture.handle(dict(type="stop", captureId=capture.capture_id))

        def until():
            observer.after(seconds)
            pressed["ms"] = capture.elapsed_ms()
            stop = threading.Thread(target=press, name="stall-direct-stop")
            stop.start()
            threads.append(stop)
            # Bound only; under the 10 s queue, so a Stop that waits for the
            # lock is still saved and its late moment recorded.
            pressed["marked"] = capture.stop_requested.wait(5)
            observer.after(seconds)  # Storage recovers later still.

        hold_lock(capture, until).join(BOUND)
        threads[0].join(BOUND)
        assert not threads[0].is_alive(), "the direct Stop never finished"
        return dict(stopAtMs=capture.stop_at_ms, pressedMs=pressed["ms"], markedInStall=pressed["marked"])

    return stall


def overrun(extra):
    def stall(capture, levels, observer):
        def until():
            levels.full()
            observer.after(extra)  # Audio the full queues can no longer take.

        hold_lock(capture, until).join(BOUND)
        return {}

    return stall


CASES = {
    "lock-3.5": (lock(3.5), "no-loss"),
    "lock-8.0": (lock(8.0), "no-loss"),
    "fsync-2.5": (slow_fsync(2.5), "no-loss"),
    "fsynceach-0.7": (slow_fsync(0.7, each=True), "no-loss"),
    "stop-2.0": (stop_during(2.0), "stop"),
    "stopdirect-2.0": (stop_direct_during(2.0), "stop"),
    "stopbrowser-2.0": (stop_direct_during(2.0, browser=True), "stop"),
    "overrun-2.0": (overrun(2.0), "limit"),
}


def main(names):
    results, failures = [], []
    for name in names or CASES:
        stall, expect = CASES[name]
        record = {}
        try:
            record = run_case(name, stall)
            if expect == "no-loss":
                assert_no_loss(record)
            elif expect == "stop":
                assert_no_loss(record)
                if "pressedMs" in record:
                    assert record["markedInStall"], "a direct Stop waited for capture.lock to mark its moment"
                    assert abs(record["stopAtMs"] - record["pressedMs"]) <= 20, record
                for track in record["tracks"]:
                    # Everything captured before Stop is saved, up to the Stop moment.
                    assert abs(track["endOffsetMs"] - record["stopAtMs"]) <= 20, (track, record["stopAtMs"])
                # D4: the end is the latest of Stop, the last event and every track end.
                latest = max(record["stopAtMs"], *(t["endOffsetMs"] for t in record["tracks"]))
                assert abs(record["end"]["atMs"] - latest) <= 1, (record["end"], latest)
            else:
                assert all(limit == 10 for limit in record["queueLimitS"].values()), record["queueLimitS"]
                assert not record["drops"], "Admission must prevent unobserved pulsesrc overruns"
                assert record["state"] == "interrupted" and record["problemKind"] == "io", record
                assert record["end"]["reason"] == "error", record["end"]
                for track in record["tracks"]:
                    writer = [g for g in track["gaps"] if g["kind"] == "writer"]
                    assert sum(g["frames"] for g in writer) == track["droppedFrames"] >= 48000, track
                    assert not track["gapFrames"], track
                    assert (
                        f"ms of {track['kind']} audio was lost because the computer could not keep up; the gaps contain silence."
                        in record["problem"]
                    )
            record["passed"] = True
            print(f"PASS {name}", flush=True)
        except Exception:
            record["passed"] = False
            record["failure"] = traceback.format_exc()
            failures.append(name)
            print(f"FAIL {name}\n{record['failure']}", flush=True)
        results.append(record)
        (artifacts / "results.json").write_text(json.dumps(results, indent=2, default=str) + "\n")
    if failures:
        raise SystemExit(f"Stall cases failed: {', '.join(failures)}")


if __name__ == "__main__":
    main(sys.argv[1:])
