# SPDX-License-Identifier: GPL-3.0-or-later
"""R93-2: a host stall must not drop native input, and Stop keeps what was captured.

Each native tap holds 10 s ahead of its synchronous writer (TAP_QUEUE), a
checkpoint fsyncs only the WAVs written since the last one, and Stop records
the audio a tap still queues from before the Stop moment. The real-pulsesrc
stall itself is tests/recording/stall.py; these cover each piece without a
sound server. The taps here are scripted: every wait is on an event the code
under test sets, with a bound only as an upper limit (E1).
"""

import base64
import json
import os
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from cassini_talk import native_recorder, native_tap  # noqa: E402
from cassini_talk.recorder import TAP_QUEUE  # noqa: E402
from cassini_talk.recording_session import RecordingSession, WAVWriter  # noqa: E402

Gst.init(None)
BOUND = 10  # seconds; an upper limit only


class Described(Exception):
    pass


def tap_descriptions(monkeypatch):
    """Each shipped native tap's description, as it is parsed."""
    seen = []

    def record(description, *_):
        seen.append(description)
        raise Described

    with monkeypatch.context() as patch:
        patch.setattr(Gst, "parse_launch", record)
        patch.setattr(Gst, "parse_bin_from_description", record)
        for kind in ("microphone", "system"):
            with pytest.raises(Described):
                native_tap.NativeTap(kind, 0, lambda *_: None, lambda *_: None).start()
            with pytest.raises(Described):
                native_recorder.NativeRecorder._branch(SimpleNamespace(_test_sources=False), kind)
    return seen


def test_each_native_tap_queues_converted_pcm_behind_explicit_frame_admission(monkeypatch):
    descriptions = tap_descriptions(monkeypatch)
    assert len(descriptions) == 4
    for description in descriptions:
        # Admission sees fixed-rate PCM frames; conversion cannot hide a drop.
        source, rest = description.split(" ! ", 1)
        assert source.startswith("pulsesrc ")
        assert rest.startswith(native_recorder.CAPTURE_CONVERT + " ! ")
        assert "audio/x-raw,format=S16LE,rate=48000,channels=1 ! " + TAP_QUEUE in rest
        pipeline = Gst.parse_launch("audiotestsrc ! " + rest.replace("appsink", "appsink async=false", 1))
        try:
            queue = pipeline.get_by_name("tap-queue")
            assert queue is not None
            # NativeQueue bounds frames before this queue. It must never block
            # its input and force an unobserved pulsesrc overrun instead.
            assert queue.get_property("max-size-time") == 0
            # Both size caps off: 200 buffers or 10 MB would cut the 10 s short.
            assert queue.get_property("max-size-buffers") == 0
            assert queue.get_property("max-size-bytes") == 0
            assert queue.get_property("leaky").value_nick == "no"
        finally:
            pipeline.set_state(Gst.State.NULL)


class Fsyncs:
    """os.fsync, counted per file path."""

    def __init__(self, monkeypatch, fail=()):
        self.paths, self.fail = [], set(fail)
        real = os.fsync

        def fsync(fd):
            path = os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
            self.paths.append(path)
            if path in self.fail:
                self.fail.discard(path)
                raise OSError("Generated fsync failure")
            return real(fd)

        monkeypatch.setattr(os, "fsync", fsync)

    def take(self):
        paths, self.paths = self.paths, []
        return paths


def test_a_wav_writer_fsyncs_only_what_was_written_since_its_last_sync(tmp_path, monkeypatch):
    path = Path(os.path.realpath(tmp_path)) / "track.wav"
    fsyncs = Fsyncs(monkeypatch)
    writer = WAVWriter(path, 48000, 1)
    writer.sync()  # The header written at creation is not durable yet.
    assert fsyncs.take() == [str(path)]
    writer.sync()
    assert fsyncs.take() == []
    writer.append(bytes(960))
    writer.sync()
    assert fsyncs.take() == [str(path)]
    writer.silence(480)
    writer.sync()
    writer.sync()
    assert fsyncs.take() == [str(path)]
    # A failed fsync stays owed: the next sync retries it and fails again.
    fsyncs.fail.add(str(path))
    writer.append(bytes(960))
    with pytest.raises(OSError):
        writer.sync()
    writer.sync()
    assert fsyncs.take() == [str(path), str(path)]
    writer.close()  # Clean: nothing to make durable.
    assert fsyncs.take() == [] and writer.file.closed
    assert path.stat().st_size == 44 + 3 * 960


def test_a_checkpoint_fsyncs_an_ended_track_once_and_then_never_again(tmp_path, monkeypatch):
    session = RecordingSession(tmp_path, source="gnome", clock=lambda: 0)
    try:
        session.add_track("microphone", "Me", "microphone", 48000, 1)
        session.add_track("system", "The others", "system", 48000, 1)
        session.append_native("microphone", bytes(960), 0)
        session.append_native("system", bytes(960), 0)
        microphone, system = (os.path.realpath(session.writers[i].file.name) for i in ("microphone", "system"))
        fsyncs = Fsyncs(monkeypatch)
        session.end_track("system")  # Its checkpoint makes its last frames durable.
        first = fsyncs.take()
        assert first.count(system) == 1 and first.count(microphone) == 1
        for _ in range(3):
            session.append_native("microphone", bytes(960), session.writers["microphone"].frames / 48)
            session.checkpoint()
            # The active WAV, session.tmp and the directory; never the ended WAV.
            paths = fsyncs.take()
            assert paths.count(microphone) == 1 and system not in paths
            assert len(paths) == 3
        session.checkpoint()  # Nothing written: only the manifest and the directory.
        assert microphone not in fsyncs.take()
    finally:
        session.finish()
    assert session.manifest["state"] == "completed"
    assert [t["frames"] for t in session.manifest["tracks"]] == [4 * 480, 480]


# Stop keeps the audio captured before it (R93-2, D47).


class ManualClock:
    """A SessionClock whose now() the test sets: the Stop boundary is exact. Each
    read is noted with its thread's name, so a test can wait for one."""

    def __init__(self, origin_ms):
        self.origin_ms, self.ms = origin_ms, 0.0
        self.reads, self.readers = threading.Condition(), set()

    def now(self):
        with self.reads:
            self.readers.add(threading.current_thread().name)
            self.reads.notify_all()
        return self.ms

    def read_by(self, name):
        """Waits (bounded) until the thread `name` has read this clock."""
        with self.reads:
            return self.reads.wait_for(lambda: name in self.readers, BOUND)

    def at(self, _monotonic_ns):
        return self.ms


class DrainingTaps:
    """Scripted taps. A heard tap delivers readiness PCM at its open; a silent
    one is armed (D58.1). stop() is the EOS drain: it reports that it began and
    returns when the test has delivered what the tap still queued."""

    def __init__(self, silent=()):
        self.silent = set(silent)
        self.taps = {}

    def __call__(self, kind, _clock, append, failed, *, level):
        taps = self

        class Tap:
            pipeline = None

            def __init__(self):
                self.kind, self.append = kind, append
                self.draining, self.drained = threading.Event(), threading.Event()

            def start(self):
                if kind not in taps.silent:
                    self.append(bytes(4), 0)  # Readiness PCM before commit.

            def stop(self):
                self.draining.set()
                assert self.drained.wait(BOUND)  # Bound only: the test ends the drain.

        tap = Tap()
        self.taps[kind] = tap
        return tap


def start_capture(tmp_path, taps, *, microphone, others_source, features=()):
    from cassini_talk.browser_capture import BrowserCapture

    origin = int(time.time() * 1000)
    clock = ManualClock(origin)
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=taps)
    message = dict(type="start", captureId="stall", timelineOriginMs=origin, microphone=microphone)
    if features:
        message["features"] = list(features)
    if others_source == "system":
        capture.browser_lost = True
        capture.start(message, others_source="system", reason="missing", session_clock=clock)
    else:
        capture.start(message, session_clock=clock)
    return capture, clock


def tone(frames, value=7):
    return struct.pack("<h", value) * frames


def stop_in_background(capture, tap):
    """Press Stop; returns once Stop's boundary is fixed and tap's drain began."""
    result = {}

    def run():
        try:
            result["stopped"] = capture.stop()
        except BaseException as error:  # pragma: no cover - reported by the join below
            result["error"] = error

    thread = threading.Thread(target=run, name="test-stop")
    thread.start()
    assert capture.stop_requested.wait(BOUND)
    assert tap.draining.wait(BOUND)
    return thread, result


def record_live(capture, clock):
    """The microphone's device delivers 10 ms buffers back to back; 600 ms reach
    the WAV live. Stop comes 345 ms later (on the capture's one clock), while
    the tap's queue still holds the buffers captured since. Returns the tap and
    its track's start."""
    identifier, tap = capture.native_taps["microphone"]
    session = capture.host.session
    start = next(t for t in session.manifest["tracks"] if t["id"] == identifier)["startOffsetMs"]
    for index in range(60):
        tap.append(tone(480), start + index * 10)
    assert session.writers[identifier].frames == 60 * 480
    clock.ms = start + 945
    return tap, start


def drain_across_stop(tap, start, clock):
    """The drain: 34 buffers captured before Stop, one straddling its moment and
    two captured after it, then EOS. Draining takes time: the clock is 2 s past
    Stop when it ends, and the end event must still be at the Stop moment (D4)."""
    for index in range(60, 97):
        tap.append(tone(480, value=9), start + index * 10)
    clock.ms = start + 2945
    tap.drained.set()


def assert_kept_before_stop(capture, start, reason="user", *, others=(), problem_kind=None):
    """`others`: the kinds of the other tracks saved (a browser span's)."""
    saved = capture.host.session.manifest
    (track,) = [t for t in saved["tracks"] if t["kind"] == "microphone"]
    assert sorted(t["kind"] for t in saved["tracks"] if t is not track) == sorted(others)
    # Everything before the boundary, and the first 5 ms of the straddling buffer.
    assert track["frames"] == 94 * 480 + 240
    assert track["endOffsetMs"] == pytest.approx(start + 945)
    assert "gapFrames" not in track and not [g for g in track.get("gaps", []) if g["kind"] != "prefix"]
    if problem_kind is None:
        assert saved["state"] == "completed" and saved.get("problemKind") is None
    else:
        assert saved.get("problemKind") == problem_kind
    assert saved["events"][-1] == dict(type="end", atMs=pytest.approx(start + 945), reason=reason)
    pcm = (capture.directory / track["file"]).read_bytes()[44:]
    assert pcm[: 60 * 960] == tone(60 * 480) and pcm[60 * 960 :] == tone(34 * 480 + 240, value=9)


def test_stop_records_the_queued_audio_captured_before_it_and_cuts_the_straddling_buffer(tmp_path):
    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    tap, start = record_live(capture, clock)
    thread, result = stop_in_background(capture, tap)  # The Stop boundary is clock.ms.
    drain_across_stop(tap, start, clock)
    thread.join(BOUND)
    assert not thread.is_alive() and "error" not in result
    assert_kept_before_stop(capture, start)


@pytest.mark.parametrize("route", ["stop", "policy", "browser-stop"])
def test_stop_pressed_while_a_stalled_checkpoint_holds_the_capture_lock_keeps_its_moment(tmp_path, route):
    """#230 review: another thread holds capture.lock (a checkpoint stalled on
    storage) when Stop is pressed at 945 ms; storage recovers 2 s later. Stop's
    moment is when it was pressed, not when the lock came free: the audio queued
    from before it is kept, the audio after it stays out, and the end is at 945 ms.
    Routes: a direct Stop, a policy lifecycle Stop (callEnded) and the browser's
    Stop message."""
    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    tap, start = record_live(capture, clock)
    held, release = threading.Event(), threading.Event()

    def hold():
        with capture.lock:
            held.set()
            assert release.wait(3 * BOUND)  # Bound only: the test releases it.

    holder = threading.Thread(target=hold, name="test-stall")
    holder.start()
    assert held.wait(BOUND)
    presses = {
        "stop": capture.stop,
        "policy": lambda: capture.stop(reason="callEnded"),
        "browser-stop": lambda: capture.handle(dict(type="stop", captureId="stall")),
    }
    result = {}

    def run():
        try:
            result["stopped"] = presses[route]()
        except BaseException as error:  # pragma: no cover - asserted below
            result["error"] = error

    thread = threading.Thread(target=run, name="test-stop")
    thread.start()
    # Stop's moment is read while the stall still holds the lock.
    marked = clock.read_by("test-stop")
    clock.ms = start + 2945  # Storage recovers 2 s after Stop.
    release.set()
    holder.join(BOUND)
    assert tap.draining.wait(BOUND)
    drain_across_stop(tap, start, clock)
    thread.join(BOUND)
    assert not thread.is_alive() and "error" not in result, result
    assert capture.stop_at_ms == pytest.approx(start + 945)
    assert marked, "Stop's moment waited for the capture lock"
    assert_kept_before_stop(capture, start, reason="callEnded" if route == "policy" else "user")


class OrderedLock:
    """capture.lock as a test orders it: each thread's first wait for it is noted,
    and the `gated` thread may take it only once `go` is set. The lock itself is
    the capture's own RLock, so every other acquisition is unchanged."""

    def __init__(self, lock, gated, go):
        self.lock, self.gated, self.go = lock, gated, go
        self.arrivals, self.waiting = threading.Condition(), set()

    def acquire(self, blocking=True, timeout=-1):
        name = threading.current_thread().name
        with self.arrivals:
            self.waiting.add(name)
            self.arrivals.notify_all()
        if name == self.gated:
            assert self.go.wait(BOUND)  # Bound only: the test sets it.
        return self.lock.acquire(blocking, timeout)

    def release(self):
        self.lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()

    def waited_by(self, name):
        """Waits (bounded) until the thread `name` asked for the lock."""
        with self.arrivals:
            return self.arrivals.wait_for(lambda: name in self.waiting, BOUND)


def guest_audio(offset_ms, frames, sequence):
    return dict(
        type="audio",
        id="guest",
        sequence=sequence,
        offsetMs=offset_ms,
        pcm=base64.b64encode(tone(frames, value=3)).decode(),
    )


def stall_holding(capture):
    """Another thread holds capture.lock, as a checkpoint stalled on storage.
    Returns (holder, release)."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with capture.lock:
            held.set()
            assert release.wait(3 * BOUND)  # Bound only: the test releases it.

    holder = threading.Thread(target=hold, name="test-stall")
    holder.start()
    assert held.wait(BOUND)
    return holder, release


def in_thread(name, call):
    result = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as error:  # asserted by the callers
            result["error"] = error

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread, result


@pytest.mark.parametrize("outcomes", [False, True], ids=["plain", "source-outcomes"])
def test_pcm_queued_behind_a_stall_that_wins_the_lock_before_a_browser_stop_saves_nothing_after_it(tmp_path, outcomes):
    """#230 review: a checkpoint stalled on storage holds capture.lock when the
    browser's Stop message arrives at 945 ms. The tap's buffers captured while
    storage stalled (600 ms to 2950 ms) and a browser PCM frame are queued on
    the same lock. When it comes free at 2950 ms they take it before the Stop
    handler does. Stop's boundary was published when it arrived: nothing after
    945 ms is saved, and the end is at 945 ms. On 979431a the queued callbacks
    saved the audio through 2950 ms, and the end was stamped there."""
    taps = DrainingTaps()
    features = ("source-outcomes-v1",) if outcomes else ()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser", features=features)
    declared = dict(type="track", id="guest", label="Guest", kind="remote", sampleRate=48000, channels=1)
    if outcomes:
        declared["sourceTrackId"] = "guest"
    assert capture.handle(declared)["type"] == "ack"
    # The guest's span delivered 10 ms before Stop.
    assert capture.handle(guest_audio(0, 480, 0))["type"] == "ack"
    tap, start = record_live(capture, clock)  # Stop's moment: start + 945.
    go = threading.Event()
    ordered = capture.lock = OrderedLock(capture.lock, "test-stop", go)
    holder, release = stall_holding(capture)
    stop = dict(type="stop", captureId="stall")
    if outcomes:
        stop["discoveredSources"] = [dict(sourceTrackId="guest", kind="remote"), dict(sourceTrackId="pip", kind="tab")]
    stopper, stopped = in_thread("test-stop", lambda: capture.handle(stop))
    assert ordered.waited_by("test-stop")  # The Stop handler waits for the lock.

    def queued():
        # Captured while storage stalled: 600 ms to 2950 ms, then the guest's
        # next frame, sent at 1500 ms.
        for index in range(60, 295):
            tap.append(tone(480, value=9), start + index * 10)
        try:
            return capture.handle(guest_audio(start + 1500, 480, 1))
        except Exception as error:  # A plain capture refuses it by raising.
            return error

    feeder, fed = in_thread("test-pcm", queued)
    assert ordered.waited_by("test-pcm")
    clock.ms = start + 2950  # Storage recovers 2 s after Stop.
    release.set()
    holder.join(BOUND)
    feeder.join(BOUND)  # The queued callbacks ran first: the Stop handler is gated.
    assert not feeder.is_alive() and "error" not in fed, fed
    go.set()
    assert tap.draining.wait(BOUND)
    tap.drained.set()  # Everything the tap queued was delivered above.
    stopper.join(BOUND)
    assert not stopper.is_alive() and "error" not in stopped, stopped
    assert stopped["value"]["type"] == "stopped", stopped
    manifest = capture.host.session.manifest
    microphone = next(t for t in manifest["tracks"] if t["kind"] == "microphone")
    guest = next(t for t in manifest["tracks"] if t["kind"] == "remote")
    # Saved through, ended at, the guest's frames: nothing after Stop.
    saved = (microphone["endOffsetMs"] - start, manifest["events"][-1]["atMs"] - start, guest["frames"])
    assert saved == (pytest.approx(945), pytest.approx(945), 480), saved
    assert capture.stop_at_ms == pytest.approx(start + 945)
    # A source-outcomes capture expects its guest: 10 ms is lowAudio (D47 file is truth).
    assert_kept_before_stop(capture, start, others=("remote",), problem_kind="capture" if outcomes else None)
    if outcomes:
        sources = {s["sourceTrackId"]: s for s in capture.host.session.manifest["captureOutcome"]["sources"]}
        assert sources["guest"]["audioMs"] == pytest.approx(10)
        assert sources["pip"]["kind"] == "tab"  # Stop's summary was merged.


@pytest.mark.parametrize("refusal", ["identity", "kind", "reason", "lost"])
def test_a_refused_or_lost_browser_stop_during_a_stall_marks_nothing(tmp_path, refusal):
    """A Stop refused (another capture's identity, a source summary that
    conflicts with a declared kind, an invalid reason) or a first Stop for a lost
    browser (Q4: the fallback) marks no Stop. The audio queued behind the stall
    after its arrival is saved, and the recording goes on."""
    from cassini_talk.recording_session import ProtocolError

    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    declared = dict(type="track", id="guest", label="Guest", kind="remote", sampleRate=48000, channels=1)
    assert capture.handle(dict(declared, sourceTrackId="guest"))["type"] == "ack"
    tap, start = record_live(capture, clock)
    holder, release = stall_holding(capture)
    stop = dict(
        identity=dict(type="stop", captureId="another"),
        kind=dict(type="stop", captureId="stall", discoveredSources=[dict(sourceTrackId="guest", kind="tab")]),
        reason=dict(type="stop", captureId="stall", reason="whenever"),
        lost=dict(type="stop", captureId="stall", failureReason="lost"),
    )[refusal]
    stopper, stopped = in_thread("test-stop", lambda: capture.handle(stop))
    assert clock.read_by("test-stop")
    assert not capture.stop_requested.is_set()
    feeder, fed = in_thread("test-pcm", lambda: [tap.append(tone(480), start + i * 10) for i in range(60, 120)])
    clock.ms = start + 2950
    release.set()
    holder.join(BOUND)
    feeder.join(BOUND)
    stopper.join(BOUND)
    assert not feeder.is_alive() and not stopper.is_alive() and "error" not in fed
    if refusal == "reason":
        assert isinstance(stopped.get("error"), ProtocolError), stopped
    elif refusal == "lost":
        assert stopped["value"]["type"] == "stopped" and capture.browser_lost  # Q4: the fallback.
    else:
        assert stopped["value"]["type"] == "error" and stopped["value"]["scope"] == "command", stopped
    assert not capture.stop_requested.is_set() and capture.stop_at_ms is None and not capture.finished
    microphone = next(t for t in capture.host.session.manifest["tracks"] if t["kind"] == "microphone")
    assert capture.host.session.writers[microphone["id"]].frames == 120 * 480  # Nothing cut.
    tap.drained.set()
    for other in taps.taps.values():
        other.drained.set()
    capture.stop()


def test_a_declaration_racing_an_admitted_browser_stop_adds_no_source_its_summary_did_not_see(tmp_path, monkeypatch):
    """A Stop's source summary is validated without the capture lock. A track
    declaration that already holds the lock (it passed the Stop check before the
    Stop arrived) and has not yet added its new source identity must not add it
    once the Stop is admitted: the summary validated is the one merged, and a
    conflicting kind cannot refuse an admitted Stop after its boundary was
    published. The declaration is refused instead."""
    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    tap, start = record_live(capture, clock)
    ledger = capture.host.session.capture_record
    observe, inside, go = ledger.observe, threading.Event(), threading.Event()

    def held_observe(*args, **kwargs):
        if threading.current_thread().name == "test-declare":
            inside.set()
            assert go.wait(BOUND)  # Bound only: the test lets it go on.
        return observe(*args, **kwargs)

    monkeypatch.setattr(ledger, "observe", held_observe)
    declared = dict(type="track", id="late", label="Late", kind="remote", sampleRate=48000, channels=1)
    declarer, declaration = in_thread("test-declare", lambda: capture.handle(dict(declared, sourceTrackId="late")))
    assert inside.wait(BOUND)  # It holds capture.lock, past the Stop check.
    stop = dict(type="stop", captureId="stall", discoveredSources=[dict(sourceTrackId="late", kind="tab")])
    stopper, stopped = in_thread("test-stop", lambda: capture.handle(stop))
    # Admitted, and its boundary published, while the declaration holds the lock.
    assert capture.stop_requested.wait(BOUND)
    assert capture.stop_at_ms == pytest.approx(start + 945)
    go.set()
    declarer.join(BOUND)
    assert not declarer.is_alive() and "error" not in declaration, declaration
    assert declaration["value"]["type"] == "error", declaration
    assert tap.draining.wait(BOUND)
    tap.drained.set()
    stopper.join(BOUND)
    assert not stopper.is_alive() and stopped["value"]["type"] == "stopped", stopped
    manifest = capture.host.session.manifest
    assert [t["kind"] for t in manifest["tracks"]] == ["microphone"]
    sources = {s["sourceTrackId"]: s["kind"] for s in manifest["captureOutcome"]["sources"]}
    assert sources == {"late": "tab"}


@pytest.fixture
def broker(tmp_path):
    """The served broker, its native taps scripted (DrainingTaps)."""
    from broker_fixture import stop_broker

    from cassini_talk.browser_service import BrowserService

    taps = DrainingTaps()
    service = BrowserService(
        tmp_path / "config.json",
        capture_parent=tmp_path / "library",
        tap_factory=taps,
        notifier=lambda _title, _stop: None,
    )
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(BOUND)
    yield service, path
    stop_broker(service, worker, path)


class LifecyclePeer:
    """test_browser_sessions.Extension, advertising page-state-v1 as the
    shipped extension does: the broker's lifecycle policy reads its captures."""

    def __init__(self, path, tabs=(3,)):
        from test_browser_service import meeting, wait_for

        from cassini_talk.browser_service import connect, request, send_packet

        self.path = path
        self.connection = connect(path, timeout=2)
        self.stream = self.connection.makefile("rwb")
        self.sessions = {}
        send_packet(self.stream, dict(role="extension", browserInstance=None))
        capabilities = ["independent-sessions-v1", "page-state-v1"]
        send_packet(self.stream, dict(type="hello", version=1, capabilities=capabilities))
        send_packet(self.stream, dict(type="status", recording=False, sessions=[]))
        send_packet(self.stream, dict(type="meetings", meetings=[meeting(tab) for tab in tabs]))
        wait_for(lambda: len(request(path=path)["meetings"]) == len(tabs))

    def receive(self):
        from cassini_talk.browser_service import read_packet

        return read_packet(self.stream)

    def status(self, **values):
        """The recording tab's status row again, with `values` (pageState...)."""
        from cassini_talk.browser_service import send_packet

        sessions = [dict(session, **values) for session in self.sessions.values()]
        send_packet(self.stream, dict(type="status", recording=True, sessions=sessions))

    def answer(self, command):
        from cassini_talk.browser_service import send_packet

        tab = command["tabId"]
        value = dict(self.sessions.pop(tab, dict(tabId=tab)), recording=False)
        result = dict(type="result", requestId=command["requestId"], ok=True, error=None)
        send_packet(self.stream, dict(result, status=dict(value, sessions=list(self.sessions.values()))))

    def close(self):
        self.stream.close()
        self.connection.close()


@pytest.mark.parametrize("stall", [False, "capture", "policy", "status"], ids=["", "stalled", "policy", "status"])
@pytest.mark.parametrize("route", ["stop-all", "stop", "command", "cancel"])
def test_each_broker_stop_path_fixes_the_stop_moment_so_queued_audio_before_it_is_kept(broker, route, stall):
    """The broker marks Stop before capture.stop() runs: GNOME's Stop and Quit
    (stop-all), a user's Stop of one call, a policy Stop of it (not the user's)
    and a cancelled capture. Each must fix the Stop moment as it marks Stop;
    marking it bare leaves no boundary, and the drain would drop every queued
    buffer captured before Stop.

    `stall`: Stop is pressed at 945 ms while a checkpoint stalled on storage
    holds capture.lock. Stop's moment is still 945 ms, marked before the stall
    ends at 2945 ms (#230 review). On 979431a stop-all took every capture's
    lock for its snapshot before it marked Stop.

    "policy" and "status": a page-state-v1 control peer (the shipped extension)
    records the call, so the broker's lifecycle policy reads the capture. The
    stall begins and Stop is pressed only once the broker has asked for
    capture.lock: its accept loop's periodic policy tick ("policy"), or the
    peer's status naming the page state ("status"), which retires a gone
    document's spans and runs the policy. On ad5d337 both waited for
    capture.lock while holding the broker's lock (and the accept loop), so
    every route marked Stop at 2945 ms, when storage recovered."""
    from test_browser_capture import bind_capture
    from test_browser_capture import start as start_audio
    from test_browser_service import wait_for
    from test_browser_sessions import Extension

    from cassini_talk.browser_audio_proxy import BrowserAudioProxy
    from cassini_talk.browser_service import request

    service, path = broker
    client = BrowserAudioProxy(path=path)
    peer = release = None
    try:
        start_audio(client, "stall", service=service)
        capture = service.captures["stall"]
        lifecycle = stall in ("policy", "status")
        if route in ("stop", "command") or lifecycle:
            peer = LifecyclePeer(path) if lifecycle else Extension(path, (3,))
            bind_capture(peer, capture)
            identifier = next(m["id"] for m in request(path=path)["meetings"] if m["tabId"] == 3)
        if lifecycle:
            wait_for(lambda: capture.lifecycle_capable)  # The broker's policy reads this capture.
        # The capture's one clock, set by the test from here on: the Stop moment is exact.
        clock = ManualClock(capture.session_clock.origin_ms)
        clock.ms = capture.session_clock.now()
        capture.session_clock = clock
        tap, start = record_live(capture, clock)
        if stall:
            ordered = capture.lock = OrderedLock(capture.lock, None, None)
            holder, release = stall_holding(capture)
        if lifecycle:
            with ordered.arrivals:
                ordered.waiting.clear()
            if stall == "status":
                peer.status(pageState="present", documentGeneration="doc-1")
            # The broker has asked for the stalled capture.lock: its accept
            # loop's policy tick, or the reader handling the peer's status.
            asker = "(serve)" if stall == "policy" else "(handle)"
            with ordered.arrivals:
                assert ordered.arrivals.wait_for(lambda: any(asker in n for n in ordered.waiting), BOUND), (
                    ordered.waiting
                )
        stops = {
            "stop-all": lambda: request("stop-all", path=path),
            "stop": lambda: request("stop", path=path, id=identifier),
            "command": lambda: service.command(identifier, "stop", user=False),
        }
        result = {}
        if route == "cancel":

            def cancel():
                with service.lock:  # Its callers hold the broker's lock.
                    service._cancel_capture("stall")

            canceller, _ = in_thread("test-cancel", cancel)
        else:

            def run():
                try:
                    result["stopped"] = stops[route]()
                except BaseException as error:  # pragma: no cover - asserted below
                    result["error"] = error

            thread = threading.Thread(target=run, name="test-broker-stop")
            thread.start()
        assert capture.stop_requested.wait(BOUND)
        if stall:
            assert capture.stop_at_ms == pytest.approx(start + 945)  # Marked while the stall goes on.
            clock.ms = start + 2945  # Storage recovers 2 s after Stop.
            release.set()
            holder.join(BOUND)
        if route == "cancel":
            canceller.join(BOUND)
        assert tap.draining.wait(BOUND)
        drain_across_stop(tap, start, clock)
        if peer is not None and route != "cancel":  # A cancel sends no command.
            command = peer.receive()  # Sent once the local save is done.
            assert (command["action"], command["tabId"]) == ("stop", 3)
            peer.answer(command)
        if route != "cancel":
            thread.join(BOUND)
            assert not thread.is_alive() and "error" not in result and "error" not in result["stopped"], result
        assert capture.stopped.wait(BOUND) and capture.finished
        assert_kept_before_stop(capture, start)
    finally:
        if release is not None:
            release.set()  # A failed case still ends its stall.
        client.close()
        if peer is not None:
            peer.close()


@pytest.mark.parametrize("step", ["tick", "ask", "cancel"])
def test_the_broker_never_waits_for_a_stalled_capture_lock_off_the_stop_path(tmp_path, step):
    """The broker's watchdog tick (on its accept loop), a resume ask (under its
    lock) and a fallback's cancel (on the policy thread) each read or write a
    capture. While a checkpoint stalled on storage holds capture.lock, none of
    them waits for it: a Stop pressed meanwhile must reach the broker and be
    marked when it is pressed (#230 review). The stale-PCM watchdog decides on
    a later tick (a busy capture is the broker's own stall, not the peer's);
    the ask is noted at once; the cancel waits off the policy thread. On
    ad5d337 the ask and the cancel waited for the stall."""
    from cassini_talk.browser_service import PCM_STALE_AFTER, AudioTransport, BrowserService

    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    declared = dict(type="track", id="guest", label="Guest", kind="remote", sampleRate=48000, channels=1)
    assert capture.handle(declared)["type"] == "ack"
    assert capture.handle(guest_audio(0, 480, 0))["type"] == "ack"
    if step == "tick":
        capture.multistream_capable = True
        transport = AudioTransport(None, lambda: 0.0)
        transport.capture = capture
        transport.last_inbound = -PCM_STALE_AFTER  # A remote span that went quiet.
        assert transport.expired(0.0, wait=False) == "stopped sending audio"  # A free lock: as before.
        call = lambda: transport.expired(0.0, wait=False)  # noqa: E731
    elif step == "ask":
        call = lambda: capture.mark_resume_ask("ask-1")  # noqa: E731
    else:
        service = BrowserService(
            tmp_path / "config.json", capture_parent=tmp_path / "library", tap_factory=taps, notifier=lambda *_: None
        )
        capture.fallback_pending = True  # A fallback decided, its device not yet open.
        call = lambda: service._cancel_fallback("stall", capture)  # noqa: E731
    holder, release = stall_holding(capture)
    try:
        caller, called = in_thread("test-" + step, call)
        caller.join(BOUND)  # Bound only: it returns while the stall goes on.
        assert not caller.is_alive(), "it waited for the stalled capture lock"
        assert "error" not in called, called
        if step == "tick":
            assert called["value"] is None  # Busy: the next tick decides.
        elif step == "ask":
            assert capture.resume_asks == {"ask-1": clock.ms}
        else:
            deferred = called["value"]
            assert deferred is not None and not capture._fallback_cancelled  # Its own thread waits.
    finally:
        release.set()
        holder.join(BOUND)
    if step == "cancel":
        deferred.join(BOUND)
        assert not deferred.is_alive() and capture._fallback_cancelled  # Done once the lock is free.
        capture.fallback_pending = False
    for tap in taps.taps.values():
        tap.drained.set()
    capture.stop()


def test_only_the_first_marked_browser_stop_may_add_sources_after_the_seal(tmp_path):
    """Two browser Stops for one capture (an old transport's and a resumed
    one's) both wait on a stalled capture.lock. Stop A arrives first: admitted,
    its moment (945 ms) marked, the identities sealed. Stop B's summary names A's
    new source with another kind; it was validated before A merged. When the
    stall ends B takes the lock first. B must not merge a source after the seal:
    it is refused, and A, whose moment is already the boundary, is never refused
    afterwards. On ad5d337 B merged first and A was refused ("Conflicting source
    kind") while its moment stayed the boundary of B's Stop."""
    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    tap, start = record_live(capture, clock)  # A arrives at 945 ms.
    go = threading.Event()
    ordered = capture.lock = OrderedLock(capture.lock, "test-stop-a", go)
    holder, release = stall_holding(capture)
    stopper_b = None
    try:
        a = dict(type="stop", captureId="stall", discoveredSources=[dict(sourceTrackId="x", kind="remote")])
        stopper_a, stopped_a = in_thread("test-stop-a", lambda: capture.handle(a))
        assert ordered.waited_by("test-stop-a")
        assert capture.stop_requested.is_set() and capture.stop_at_ms == pytest.approx(start + 945)
        clock.ms = start + 1500
        b = dict(type="stop", captureId="stall", discoveredSources=[dict(sourceTrackId="x", kind="tab")])
        stopper_b, stopped_b = in_thread("test-stop-b", lambda: capture.handle(b))
        assert ordered.waited_by("test-stop-b")
        clock.ms = start + 2950
        release.set()
        holder.join(BOUND)
        stopper_b.join(BOUND)  # B takes the lock first: A is gated.
        assert not stopper_b.is_alive(), "B was not refused: it merged and stopped" if tap.draining.is_set() else ""
        assert "error" not in stopped_b, stopped_b
        assert stopped_b["value"]["type"] == "error" and stopped_b["value"]["scope"] == "command", stopped_b
        assert not capture.finished
    finally:
        release.set()
        go.set()
    assert tap.draining.wait(BOUND)
    drain_across_stop(tap, start, clock)
    stopper_a.join(BOUND)
    assert not stopper_a.is_alive() and stopped_a.get("value", {}).get("type") == "stopped", stopped_a
    sources = {s["sourceTrackId"]: s["kind"] for s in capture.host.session.manifest["captureOutcome"]["sources"]}
    assert sources == {"x": "remote"}  # A's summary, merged.
    assert_kept_before_stop(capture, start, problem_kind="capture")  # x was expected and never heard.


def test_a_later_stop_that_finishes_the_capture_first_saves_the_admitted_stops_summary(tmp_path):
    """#230 Codex: Stop A arrives at 945 ms with a summary naming an expected
    remote source (x) and is admitted, then waits on a stalled capture.lock.
    Stop B, with an empty summary, takes the lock first and finishes the
    capture. The save must include A's summary (x: remote, expected, never
    heard), and A's reply is the saved result, as B's is. On 6cd9a9d B saved
    captureOutcome.sources [] and A got "Capture already saved"."""
    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    tap, start = record_live(capture, clock)  # A arrives at 945 ms.
    go = threading.Event()
    ordered = capture.lock = OrderedLock(capture.lock, "test-stop-a", go)
    holder, release = stall_holding(capture)
    try:
        a = dict(type="stop", captureId="stall", discoveredSources=[dict(sourceTrackId="x", kind="remote")])
        stopper_a, stopped_a = in_thread("test-stop-a", lambda: capture.handle(a))
        assert ordered.waited_by("test-stop-a")
        assert capture.stop_requested.is_set() and capture.stop_at_ms == pytest.approx(start + 945)
        clock.ms = start + 1500
        b = dict(type="stop", captureId="stall", discoveredSources=[])
        stopper_b, stopped_b = in_thread("test-stop-b", lambda: capture.handle(b))
        assert ordered.waited_by("test-stop-b")
        clock.ms = start + 2950
        release.set()
        holder.join(BOUND)
        assert tap.draining.wait(BOUND)  # B took the lock first and is finishing; A is gated.
        drain_across_stop(tap, start, clock)
        stopper_b.join(BOUND)
        assert not stopper_b.is_alive() and "error" not in stopped_b, stopped_b
        assert stopped_b["value"] == dict(type="stopped", path=str(capture.directory)), stopped_b
        saved = json.loads((capture.directory / "session.json").read_text())
        assert saved["state"] == "interrupted" and saved["problemKind"] == "capture"
        (source,) = saved["captureOutcome"]["sources"]  # B finished it: A's summary is in the save.
        assert source["sourceTrackId"] == "x" and source["kind"] == "remote"
        assert source["captureOutcome"] == "neverAudio" and "captureExpected" not in source
    finally:
        release.set()
        go.set()
    stopper_a.join(BOUND)
    assert not stopper_a.is_alive() and "error" not in stopped_a, stopped_a
    assert stopped_a["value"] == stopped_b["value"], stopped_a  # Its summary was saved: the saved result.
    assert_kept_before_stop(capture, start, problem_kind="capture")  # x was expected and never heard.
    # A Stop after the save still adds nothing.
    late = dict(type="stop", captureId="stall", discoveredSources=[dict(sourceTrackId="y", kind="tab")])
    assert capture.handle(late)["message"] == "Capture already saved"
    assert [s["sourceTrackId"] for s in capture.host.session.manifest["captureOutcome"]["sources"]] == ["x"]


@pytest.mark.parametrize("first_ms, opens", [(-20, True), (-5, True), (0, False), (10, False)])
def test_an_armed_tap_whose_first_buffer_was_queued_before_stop_opens_its_track(tmp_path, first_ms, opens):
    """D58.1: a silent system tap is armed. Its first buffer, captured before
    Stop but still queued behind a stall, opens its track during the drain."""
    taps = DrainingTaps(silent={"system"})
    capture, clock = start_capture(tmp_path, taps, microphone=False, others_source="system")
    identifier, tap = capture.native_taps["system"]
    assert capture.snapshot()["armed"] == {"system": capture._tap_states[identifier]["at"]}
    clock.ms = capture._tap_states[identifier]["at"] + 2000
    boundary = clock.ms  # Stop's moment, on the capture's one clock.
    thread, result = stop_in_background(capture, tap)
    tap.append(tone(480), boundary + first_ms)
    tap.append(tone(480), boundary + first_ms + 10)
    clock.ms = boundary + 2000  # The drain took 2 s.
    tap.drained.set()
    thread.join(BOUND)
    assert not thread.is_alive() and "error" not in result
    assert capture.nothing_recorded is not opens
    if opens:
        (track,) = capture.host.session.manifest["tracks"]
        assert track["startOffsetMs"] == pytest.approx(boundary + first_ms)
        assert track["endOffsetMs"] == pytest.approx(boundary)
        assert capture.host.session.manifest["events"][-1]["atMs"] == pytest.approx(boundary)
        assert result["stopped"]["path"] is not None
    else:
        assert result["stopped"]["path"] is None and capture.discarded


def test_a_buffer_after_the_session_finished_is_never_written(tmp_path):
    """Before Stop's moment, but after the save: a late callback (the pipeline not
    yet gone) writes nothing, and an armed tap's first buffer opens nothing."""
    taps = DrainingTaps(silent={"system"})
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="system")
    microphone, tap = capture.native_taps["microphone"]
    system = taps.taps["system"]
    session = capture.host.session
    start = session.manifest["tracks"][0]["startOffsetMs"]
    tap.append(tone(480), start)
    clock.ms = start + 1000
    tap.drained.set()
    system.drained.set()
    capture.stop()
    assert session.finished
    tap.append(tone(480), start + 10)
    system.append(tone(480), start + 10)
    assert session.writers[microphone].frames == 480
    assert [t["kind"] for t in session.manifest["tracks"]] == ["microphone"]


def test_the_native_recorder_keeps_the_stop_moment_through_a_held_pcm_lock(tmp_path):
    """The native-only recorder: a stalled checkpoint holds its PCM lock when Stop
    is pressed, and the stall lasts 1 s of audio past Stop. Its taps capture into
    their queues until the drain's EOS reaches them, after the lock is free. The
    audio queued before Stop is kept up to Stop's moment, the audio after it stays
    out, and the end event is at that moment, not at the drain's end (D4)."""
    from test_recorder import spin_until

    recorder = native_recorder.NativeRecorder()
    finished, errors = [], []
    recorder.connect("finished", lambda _r, path: finished.append(path))
    recorder.connect("error", lambda _r, message: errors.append(message))
    holder = None
    release = threading.Event()
    try:
        recorder.start(tmp_path, test=True, observe_output=False)
        spin_until(
            lambda: (
                not recorder.starting
                and len(recorder._track_ids) == 2
                and all(t["frames"] >= 9600 for t in recorder.session.manifest["tracks"])
            )
        )
        pipeline = recorder.pipeline
        # Audio time reaching each tap's queue: the newest input's end, as it enters.
        reached, arrivals = {}, threading.Condition()

        def entering(_pad, info, kind):
            buffer = info.get_buffer()
            with arrivals:
                reached[kind] = (buffer.pts + buffer.duration) / Gst.MSECOND
                arrivals.notify_all()
            return Gst.PadProbeReturn.OK

        for kind, branch in recorder._branches.items():
            pad = branch.get_by_name("tap-queue").get_static_pad("sink").get_peer()
            pad.add_probe(Gst.PadProbeType.BUFFER, entering, kind)
        held = threading.Event()

        def hold():
            with recorder._lock:
                held.set()
                assert release.wait(BOUND)  # Bound only: the test releases it.

        holder = threading.Thread(target=hold, name="test-stall")
        holder.start()
        assert held.wait(BOUND)
        pressed = recorder._elapsed_ms(pipeline)
        recorder.stop()
        with arrivals:  # The stall goes on for 1 s of audio past Stop.
            assert arrivals.wait_for(lambda: len(reached) == 2 and min(reached.values()) >= pressed + 1000, BOUND)
        release.set()
        holder.join(BOUND)
        spin_until(lambda: bool(finished))
        manifest = recorder.session.manifest
        assert not errors and manifest["state"] == "completed", (errors, manifest.get("problem"))
        ends = {t["kind"]: t["endOffsetMs"] for t in manifest["tracks"]}
        # Everything before Stop is kept and nothing after it. The test source
        # generates each 1024-frame buffer (21.3 ms) at its start: the one
        # written whole just before Stop can end up to that much after it.
        slack = 1024 * 1000 / 48000
        assert all(pressed - slack <= end <= pressed + slack for end in ends.values()), (pressed, ends)
        # D4: the latest of Stop's moment, the last event and every track end.
        end = manifest["events"][-1]
        assert end["type"] == "end" and end["reason"] == "user"
        assert end["atMs"] == pytest.approx(max(recorder._stop_at_ms, *ends.values())), (pressed, end, ends)
    finally:
        release.set()
        if holder is not None:
            holder.join(BOUND)
        if recorder.pipeline is not None:
            recorder._begin_finish()
            spin_until(lambda: recorder.pipeline is None)
        recorder._executor.shutdown()
