# SPDX-License-Identifier: GPL-3.0-or-later
"""D58.1: the others' side (computer sound) stays armed through silence.

It is never refused for silence or a missing buffer: it opens on its first
buffer however late, and only an explicit error refuses it, with its own D58
code. The microphone keeps its 3 s bound. The devices are scripted appsrc
branches on a fake open clock (test_native_sides.Sides): no sound server, and
no wall-time wait stands in for a device event.
"""

import json
import logging
import struct
import threading
import time

import pytest
from durable import DURABLE_BOUND

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("GStreamer typelib is unavailable", allow_module_level=True)
from gi.repository import GLib, Gst  # noqa: E402
from test_native_sides import (  # noqa: E402
    BOUND,
    CAUSES,
    FRAME,
    FRAME_NS,
    Sides,
    browser_start,
    close,
    kinds_of,
    native_pcm,
    refusals,
)
from test_recorder import spin_until  # noqa: E402

from cassini_talk import refusals as R  # noqa: E402
from cassini_talk.native_recorder import NativeRecorder  # noqa: E402

Gst.init(None)
DENIED = Gst.ResourceError.NOT_AUTHORIZED
QUIET = {"microphone": ("ok", 0.1), "system": ("never", 0)}


def dispatch_until(check, message):
    context = GLib.MainContext.default()
    expired = []

    def expire(*_args):
        expired.append(True)
        return GLib.SOURCE_REMOVE

    guard = GLib.timeout_source_new(int(DURABLE_BOUND * 1000))
    guard.set_callback(expire)
    guard.attach(context)
    try:
        while not check() and not expired:
            context.iteration(True)
        assert check(), message
    finally:
        guard.destroy()


def start(tmp_path, plan, causes=None, codes=None):
    recorder = NativeRecorder()
    sides = Sides(recorder, plan, causes, codes)
    signals = []
    for name in ("started", "error", "finished"):
        recorder.connect(name, lambda _r, value, name=name: signals.append((name, value)))
    recorder.start(tmp_path, test=True)
    spin_until(lambda: not recorder.starting)
    return recorder, sides, signals


def names(signals):
    return [name for name, _ in signals]


def live(session):
    manifest = session.manifest
    tracks = [(t["kind"], t.get("ended"), t.get("startOffsetMs")) for t in manifest["tracks"]]
    return R.live_lines(manifest.get("refusals"), tracks, manifest.get("events") or ())


def post_error(sides, kind, code, cause="Fake monitor went away"):
    """An explicit device error on a side's current branch, then its stream's end."""
    source = sides.sources[kind]
    for problem in (cause, "Internal data stream error."):
        error = GLib.Error.new_literal(Gst.ResourceError.quark(), problem, int(code))
        source.post_message(Gst.Message.new_error(source, error, "scripted"))
    source.emit("end-of-stream")


def saved(session):
    return json.loads((session.directory / "session.json").read_text())


def stop(recorder, signals):
    recorder.stop()
    dispatch_until(
        lambda: "finished" in names(signals) and recorder.pipeline is None and not recorder._finishing,
        "the armed-side capture did not finish",
    )


def test_a_silent_others_side_stays_armed_and_opens_on_its_late_first_buffer(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        session = recorder.session
        # The start is decided at the others' 3 s window: no refusal, no track yet.
        assert names(signals) == ["started"] and sides.started_at == BOUND
        assert kinds_of(session) == ["microphone"] and "refusals" not in session.manifest
        assert recorder.refusals == [] and not refusals(caplog) and live(session) == []
        assert recorder._pending_sources["system"]["armed"]
        # Much later on the open clock (well past any bound), the computer sounds.
        sides.now = BOUND + 7.5
        assert sides.fire("system") == Gst.FlowReturn.OK
        spin_until(lambda: "system" in recorder._track_ids)
        spin_until(lambda: session.writers["system"].frames == 480)  # That first buffer is recorded.
        assert kinds_of(session) == ["microphone", "system"] and not recorder._pending_sources
        assert [t["file"] for t in session.manifest["tracks"]] == ["track-microphone.wav", "track-system.wav"]
        system = session.manifest["tracks"][1]
        assert system["startOffsetMs"] >= session.manifest["tracks"][0]["startOffsetMs"]
        sides.feed("system", 9)
        sides.feed("microphone", 10)
        spin_until(lambda: session.writers["system"].frames == 4800)
        assert live(session) == [] and "refusals" not in session.manifest
    finally:
        close(recorder)
    stored = saved(session)
    assert stored["state"] == "completed" and "problem" not in stored and "refusals" not in stored
    assert [t["kind"] for t in stored["tracks"]] == ["microphone", "system"]
    assert recorder.unheard == set() and not refusals(caplog)


def test_others_silent_until_stop_leave_no_track_no_refusal_and_no_failure_line(tmp_path, caplog):
    from cassini_talk.capture_details import read_details
    from cassini_talk.library import LibraryScanner

    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    session = recorder.session
    try:
        sides.feed("microphone", 99)
        spin_until(lambda: session.writers["microphone"].frames == 48000)
        assert live(session) == [] and recorder.refusals == []
        stop(recorder, signals)
    finally:
        close(recorder)
    # The folder: a microphone track only, no refusal, a completed recording.
    assert names(signals) == ["started", "finished"] and not recorder.discarded
    stored = saved(session)
    assert [t["kind"] for t in stored["tracks"]] == ["microphone"]
    assert "refusals" not in stored and stored["state"] == "completed" and "problem" not in stored
    assert [e for e in stored["events"] if e["type"] != "end"] == [
        dict(atMs=0, type="othersSource", source="system", reason="missing"),
        dict(atMs=0, type="selection", me=True, others=True, reason="start"),
    ]
    assert not (session.directory / "track-system.wav").exists()
    assert recorder.unheard == {"system"} and not refusals(caplog)
    # Details: no refusal or failure line, nor any permission copy; The others
    # simply have no saved audio.
    meeting = next(m for m in LibraryScanner(tmp_path).scan().meetings if m.directory == session.directory)
    report = read_details(meeting)
    assert report.refusals == ()
    assert not any(
        line.warning or line.id.startswith(("details.sources.refused", "details.sources.failed"))
        for line in report.audio
    )
    copied = report.plain_text()
    assert "couldn’t be started" not in copied and "ermission" not in copied
    assert [s.side for s in report.spans] == ["Me"]


@pytest.mark.parametrize(
    ("kind", "code", "expected"),
    [
        ("system", DENIED, "permissionDenied"),
        ("system", Gst.ResourceError.OPEN_READ, "deviceUnavailable"),
        ("system", Gst.ResourceError.NOT_FOUND, "deviceUnavailable"),
        # The microphone keeps its D58 classification.
        ("microphone", DENIED, "deviceUnavailable"),
    ],
)
def test_an_explicit_error_within_the_window_refuses_with_its_own_code(tmp_path, kind, code, expected):
    other = "microphone" if kind == "system" else "system"
    recorder, _sides, signals = start(tmp_path, {kind: ("error", 0.5), other: ("ok", 0.1)}, CAUSES, {kind: code})
    try:
        session = recorder.session
        assert names(signals) == ["started"] and kinds_of(session) == [other]
        assert [(r["side"], r["code"], r["atMs"]) for r in session.manifest["refusals"]] == [
            (R.SIDES[kind], expected, 0)
        ]
    finally:
        close(recorder)


def test_a_failed_state_change_refuses_the_others_as_unavailable(tmp_path):
    recorder, _sides, _signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        assert recorder.session.manifest["refusals"] == [
            dict(atMs=0, side="others", code="deviceUnavailable", message="Could not start native audio capture")
        ]
    finally:
        close(recorder)


@pytest.mark.parametrize(
    ("code", "expected"), [(DENIED, "permissionDenied"), (Gst.ResourceError.BUSY, "deviceUnavailable")]
)
def test_an_explicit_error_after_the_window_refuses_an_armed_side_at_its_start(tmp_path, caplog, code, expected):
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        session = recorder.session
        assert live(session) == []
        sides.now = BOUND + 20
        post_error(sides, "system", code)
        spin_until(lambda: "refusals" in session.manifest and not recorder._pending_sources)
        assert [(r["atMs"], r["side"], r["code"]) for r in session.manifest["refusals"]] == [(0, "others", expected)]
        assert recorder.refusals == session.manifest["refusals"]
        # Only now is there a line for the others; the microphone records on.
        assert [line["id"] for line in live(session)] == ["errors.deviceUnavailable.others"]
        assert names(signals) == ["started"] and not recorder._finishing
        sides.feed("microphone", 10)
        spin_until(lambda: session.writers["microphone"].frames == 5280)
        assert refusals(caplog) == [
            "Native others side (system) did not open: " + session.manifest["refusals"][0]["message"]
        ]
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed" and names(signals) == ["started", "finished"]


def test_a_refused_microphone_with_silent_others_records_until_stop_then_leaves_no_folder(tmp_path):
    recorder, sides, signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("never", 0)})
    try:
        session = recorder.session
        # Armed others: a recording, with the microphone's line only.
        assert names(signals) == ["started"] and kinds_of(session) == []
        assert [(r["side"], r["code"]) for r in session.manifest["refusals"]] == [("me", "deviceUnavailable")]
        assert [line["side"] for line in live(session)] == ["me"]
        stop(recorder, signals)
        # Nothing ever opened (D58 point 2): no folder, and the start banner names
        # only the refused microphone, never the silent others.
        assert recorder.discarded and not session.directory.exists() and not list(tmp_path.iterdir())
        assert names(signals) == ["started", "error", "finished"] and signals[-1] == ("finished", "")
        banner = R.banner(recorder.refusals)
        assert [(r["side"], r["code"]) for r in banner["refusals"]] == [("me", "deviceUnavailable")]
        assert recorder.unheard == {"system"}
    finally:
        close(recorder)


def test_a_refused_microphone_and_late_sounding_others_keep_the_recording(tmp_path):
    recorder, sides, signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("never", 0)})
    try:
        session = recorder.session
        sides.now = BOUND + 10
        sides.fire("system")
        spin_until(lambda: session.writers.get("system") is not None and session.writers["system"].frames == 480)
        assert live(session) == [dict(side="me", id="errors.deviceUnavailable.me", caption=None)]
        stop(recorder, signals)
    finally:
        close(recorder)
    stored = saved(session)
    assert [t["kind"] for t in stored["tracks"]] == ["system"] and stored["state"] == "completed"
    assert [(r["side"], r["code"]) for r in stored["refusals"]] == [("me", "deviceUnavailable")]
    assert not recorder.discarded and names(signals) == ["started", "finished"]


def test_a_refused_microphone_and_a_late_others_error_record_nothing(tmp_path):
    recorder, sides, signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("never", 0)})
    try:
        session = recorder.session
        post_error(sides, "system", DENIED)
        dispatch_until(lambda: "finished" in names(signals), "the armed-side capture did not finish")
        assert recorder.discarded and not session.directory.exists()
        assert [(r["side"], r["code"]) for r in recorder.refusals] == [
            ("me", "deviceUnavailable"),
            ("others", "permissionDenied"),
        ]
        assert names(signals) == ["started", "error", "finished"]
    finally:
        close(recorder)


def turn_on(recorder, me, others):
    future = recorder.select(me, others)
    spin_until(lambda: future.done() and not recorder._changing_sources)
    return future.result()


@pytest.fixture
def quiet_turn_on():
    """A turn-on's window passes without a buffer (the fake clock, not wall time)."""

    def wait(event, _timeout):
        return event.is_set()

    return wait


def test_a_silent_turn_on_selects_the_others_and_opens_on_a_late_buffer(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("ok", 0.2)})
    try:
        session = recorder.session
        turn_on(recorder, True, False)
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)  # The turned-on monitor is silent.
        state = turn_on(recorder, True, True)
        assert (state.me, state.others) == (True, True)
        at = session.manifest["events"][-1]["atMs"]
        assert session.manifest["events"][-2:] == [
            dict(atMs=at, type="selection", me=True, others=True, reason="user"),
            dict(atMs=at, type="othersSource", source="system", reason="user"),
        ]
        # Selected, armed, no track and no refusal.
        assert [t["kind"] for t in session.manifest["tracks"] if not t.get("ended")] == ["microphone"]
        assert "refusals" not in session.manifest and recorder._pending_sources["system"]["armed"]
        assert live(session) == []
        # The same selection is not reopened while it is armed.
        armed = recorder._pending_sources["system"]
        assert turn_on(recorder, True, True) == state and recorder._pending_sources["system"] is armed
        sides.fire("system")
        spin_until(lambda: recorder._track_ids.get("system", "system") != "system")
        identifier = recorder._track_ids["system"]
        spin_until(lambda: session.writers[identifier].frames == 480)
        track = next(t for t in session.manifest["tracks"] if t["id"] == identifier)
        assert track["startOffsetMs"] >= at and not track.get("ended")
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed" and "refusals" not in saved(session)


def test_an_armed_turn_on_can_be_turned_off_again(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("ok", 0.2)})
    try:
        session = recorder.session
        turn_on(recorder, True, False)
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)
        turn_on(recorder, True, True)
        branch = recorder._pending_sources["system"]["branch"]
        state = turn_on(recorder, True, False)
        assert (state.me, state.others) == (True, False)
        assert session.manifest["events"][-1]["type"] == "othersSource"
        assert session.manifest["events"][-1]["source"] == "none"
        assert not recorder._pending_sources and branch.get_parent() is None
        assert [t["kind"] for t in session.manifest["tracks"]] == ["microphone", "system"]  # Only the first span.
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed"


def test_a_turned_off_armed_branch_still_delivering_never_feeds_the_next_turn_on(tmp_path, quiet_turn_on):
    # A side armed at start whose state change has not returned yet: turning it
    # off detaches its branch, and its own thread stops it later (_detach). Until
    # then it can still deliver, after a turn-on armed a new branch for the side.
    recorder, sides, signals = start(tmp_path, QUIET)
    old = recorder._pending_sources["system"]
    try:
        session = recorder.session
        recorder._turn_on_wait = quiet_turn_on
        source, sink = sides.sources["system"], old["branch"].get_by_name("system")
        with recorder._lock:
            old["settled"] = False
        turn_on(recorder, True, False)
        assert old["retired"] and old["branch"].get_parent() is None
        turn_on(recorder, True, True)
        new = recorder._pending_sources["system"]
        assert new is not old and new["armed"] and recorder.armed_sides().keys() == {"system"}
        handled = threading.Event()  # Connected after the recorder's own handler: runs once it returned.
        sink.connect("new-sample", lambda *_: (handled.set(), Gst.FlowReturn.OK)[1])

        def late():
            handled.clear()
            buffer = Gst.Buffer.new_wrapped(FRAME)
            clock = recorder.pipeline.get_clock()
            buffer.pts = clock.get_time() - recorder.pipeline.get_base_time()
            assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK
            assert handled.wait(5)  # Bound only: the retired branch's buffer was handled.

        late()
        assert recorder._pending_sources.get("system") is new and not new["committed"] and not new["ready"].is_set()
        assert kinds_of(session) == ["microphone"] and "refusals" not in session.manifest
        # The new branch's own first buffer opens the side; the old one's never reaches its track.
        sides.fire("system")
        spin_until(lambda: recorder._track_ids.get("system") is not None)
        identifier = recorder._track_ids["system"]
        spin_until(lambda: session.writers[identifier].frames == 480)
        late()
        assert session.writers[identifier].frames == 480 and not recorder._finishing
        stop(recorder, signals)
    finally:
        old["branch"].set_state(Gst.State.NULL)
        close(recorder)
    stored = saved(session)
    assert stored["state"] == "completed" and "refusals" not in stored and "problem" not in stored
    assert [(t["kind"], t["frames"]) for t in stored["tracks"]] == [("microphone", 480), ("system", 480)]


def test_an_armed_refusal_persists_off_the_main_loop_and_stop_still_completes(tmp_path, caplog):
    # The refusal's checkpoint (manifest write and fsync, under the PCM lock)
    # is blocked as on slow storage: the main loop must keep running, and Stop
    # must be accepted, then complete once storage returns.
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    session = recorder.session
    entered, release = threading.Event(), threading.Event()
    blocked, refused, released = [], [], []
    checkpoint, refuse = session.checkpoint, session.refuse

    def slow_checkpoint():
        if "refusals" in session.manifest and not blocked:
            blocked.append(threading.current_thread() is threading.main_thread())
            entered.set()
            released.append(release.wait(5))  # Bound only: the test releases it.
        checkpoint()

    def counted_refuse(*items):
        refused.append(items)
        return refuse(*items)

    session.checkpoint, session.refuse = slow_checkpoint, counted_refuse
    threads = on_main_loop(recorder)
    try:
        sides.now = BOUND + 20
        post_error(sides, "system", DENIED)
        spin_until(entered.is_set)
        assert blocked == [False], "the refusal's checkpoint ran on the main loop"
        ran = threading.Event()
        GLib.idle_add(ran.set)
        dispatch_until(
            ran.is_set, "the main context did not dispatch the idle"
        )  # The main loop iterates while the fsync is blocked.
        # Stop is accepted at once: its work queues behind the refusal. The
        # stream error that follows the cause reaches the bus meanwhile; its
        # handling waits for the lock off the main loop too.
        recorder.stop()
        assert recorder.stopping and not released and "finished" not in names(signals)
        ran.clear()
        GLib.idle_add(ran.set)
        dispatch_until(ran.is_set, "the main context did not dispatch the idle")
        release.set()
        dispatch_until(
            lambda: "finished" in names(signals) and recorder.pipeline is None and not recorder._finishing,
            "the armed-side capture did not finish",
        )
    finally:
        release.set()
        close(recorder)
    assert names(signals) == ["started", "finished"] and not recorder.discarded
    assert released == [True], "the main loop waited for the blocked checkpoint"
    assert threads and all(threads), "a refusal's UI signal was emitted off the main loop"
    assert len(refused) == 1 and len(blocked) == 1
    stored = saved(session)
    assert stored["state"] == "completed" and "problem" not in stored
    assert [(r["atMs"], r["side"], r["code"]) for r in stored["refusals"]] == [(0, "others", "permissionDenied")]
    assert recorder.refusals == stored["refusals"] and [t["kind"] for t in stored["tracks"]] == ["microphone"]
    assert refusals(caplog) == ["Native others side (system) did not open: " + stored["refusals"][0]["message"]]


def on_main_loop(recorder):
    """Whether each "changed" signal was emitted on the main thread, in order."""
    threads = []
    recorder.connect("changed", lambda *_: threads.append(threading.current_thread() is threading.main_thread()))
    return threads


def watch_armed_failed(recorder):
    """An event set once each armed refusal's IO-worker run has returned."""
    done, armed_failed = threading.Event(), recorder._armed_failed

    def watched(*args):
        try:
            return armed_failed(*args)
        finally:
            done.set()

    recorder._armed_failed = watched
    return done


def bus_error(recorder, source, code, cause="Fake monitor went away"):
    """What GTK's bus watch does with a device's error and its stream error:
    hand them to the recorder, with no main-loop turn in between."""
    for problem in (cause, "Internal data stream error."):
        error = GLib.Error.new_literal(Gst.ResourceError.quark(), problem, int(code))
        recorder._message(None, Gst.Message.new_error(source, error, "scripted"))


def test_an_armed_refusal_whose_checkpoint_fails_is_logged_and_still_releases_its_branch(tmp_path, caplog):
    # The refusal runs on the IO worker, whose future nothing reads. When its
    # checkpoint fails (a full disk), the failure is logged, the refused branch
    # is still released, and Stop ends on the pipeline's EOS, not its timeout.
    caplog.set_level(logging.WARNING, logger="cassini_talk.native_recorder")
    recorder, sides, signals = start(tmp_path, QUIET)
    session = recorder.session
    attempt = recorder._pending_sources["system"]
    done, threads = watch_armed_failed(recorder), on_main_loop(recorder)

    def full(*_items):
        raise OSError("No space left on device")

    session.refuse = full
    try:
        sides.now = BOUND + 20
        bus_error(recorder, sides.sources["system"], DENIED)
        assert done.wait(5)  # Bound only: the worker's refusal has returned.
        assert attempt["retired"] and attempt["branch"].get_parent() is None
        failures = [r for r in caplog.records if r.getMessage() == "Could not record an armed refusal"]
        assert len(failures) == 1 and isinstance(failures[0].exc_info[1], OSError)
        ran = threading.Event()
        GLib.idle_add(ran.set)
        dispatch_until(ran.is_set, "the main context did not dispatch the idle")
        assert threads and all(threads) and not recorder._finishing
        recorder.stop()
        dispatch_until(
            lambda: "finished" in names(signals) and recorder.pipeline is None and not recorder._finishing,
            "the armed-side capture did not finish",
        )
    finally:
        close(recorder)
    assert names(signals) == ["started", "finished"] and all(threads)
    stored = saved(session)
    assert stored["state"] == "completed" and "problem" not in stored and "refusals" not in stored
    assert [(t["kind"], t["frames"]) for t in stored["tracks"]] == [("microphone", 480)]


@pytest.mark.parametrize("others", [False, True])
def test_a_try_again_after_the_worker_found_nothing_open_commits_nothing(tmp_path, others):
    # The microphone was refused at start and the armed others now fail: the
    # IO worker decides that nothing opened and queues the finish to the main
    # loop. A Try again the user sent before that finish ran must not commit a
    # side, which the finish would then end as "nothing opened".
    recorder, sides, signals = start(tmp_path, {"microphone": ("fail", 0), "system": ("never", 0)})
    session = recorder.session
    try:
        assert recorder.armed_sides() == {"system": 0} and kinds_of(session) == []
        del sides.plan["microphone"]  # Try again finds a working (test) microphone,
        if others:
            del sides.plan["system"]  # and computer sound that delivers.
        done = watch_armed_failed(recorder)
        sides.now = BOUND + 20
        bus_error(recorder, sides.sources["system"], DENIED)
        assert done.wait(5)  # Bound only: the worker decided.
        assert recorder._discard and not recorder._finishing
        future = recorder.select(True, others)  # The main loop has not run the finish yet.
        with pytest.raises(ValueError):
            future.result(10)  # Bound only: the turn-on opens its devices, then commits nothing.
        assert kinds_of(session) == [] and not recorder._pending_sources
        dispatch_until(
            lambda: "finished" in names(signals) and recorder.pipeline is None and not recorder._finishing,
            "the armed-side capture did not finish",
        )
    finally:
        close(recorder)
    assert recorder.discarded and not session.directory.exists() and not list(tmp_path.iterdir())
    assert names(signals) == ["started", "error", "finished"] and signals[-1] == ("finished", "")
    assert [(r["side"], r["code"]) for r in recorder.refusals] == [
        ("me", "deviceUnavailable"),
        ("others", "permissionDenied"),
    ]


def test_an_eos_never_overtakes_a_committed_side_s_error_still_waiting_for_the_lock(tmp_path):
    # A committed side's error waits on the bus worker for the PCM lock (a
    # writer's checkpoint holds it); the pipeline's EOS that follows must not
    # finish the recording first, as if it had ended cleanly.
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("ok", 0.2)})
    session = recorder.session
    try:
        assert kinds_of(session) == ["microphone", "system"]
        error = GLib.Error.new_literal(
            Gst.ResourceError.quark(), "Fake microphone went away", int(Gst.ResourceError.READ)
        )
        with recorder._lock:
            recorder._message(None, Gst.Message.new_error(sides.sources["microphone"], error, "scripted"))
            recorder._message(None, Gst.Message.new_eos(recorder.pipeline))
            ran = threading.Event()
            GLib.idle_add(ran.set)
            dispatch_until(ran.is_set, "the main context did not dispatch the idle")
            assert not recorder._finishing  # Neither has acted while the error waits.
        dispatch_until(
            lambda: "finished" in names(signals) and recorder.pipeline is None and not recorder._finishing,
            "the armed-side capture did not finish",
        )
    finally:
        close(recorder)
    assert names(signals) == ["started", "error", "finished"] and ("error", "Fake microphone went away") in signals
    stored = saved(session)
    assert stored["state"] == "interrupted" and stored["problem"] == "Fake microphone went away"


def test_a_turned_off_branch_s_failing_sample_neither_refuses_nor_ends_the_side_s_next_turn_on(tmp_path, quiet_turn_on):
    # As above, a turned-off armed branch still delivers; this time its sample
    # fails. Only the side's current branch may refuse it or end the recording.
    recorder, sides, signals = start(tmp_path, QUIET)
    old = recorder._pending_sources["system"]
    try:
        session = recorder.session
        recorder._turn_on_wait = quiet_turn_on
        source, sink = sides.sources["system"], old["branch"].get_by_name("system")
        with recorder._lock:
            old["settled"] = False
        turn_on(recorder, True, False)
        turn_on(recorder, True, True)
        new = recorder._pending_sources["system"]
        assert new is not old and new["armed"]
        input_pts = recorder._input_pts

        def failing(sink_, identifier, pts, frames):
            if sink_ is sink:
                raise ValueError("Scripted failure of the turned-off branch")
            return input_pts(sink_, identifier, pts, frames)

        recorder._input_pts = failing
        handled = threading.Event()  # Connected after the recorder's own handler: runs once it returned.
        sink.connect("new-sample", lambda *_: (handled.set(), Gst.FlowReturn.OK)[1])

        def late_failure():
            handled.clear()
            buffer = Gst.Buffer.new_wrapped(FRAME)
            buffer.pts = recorder.pipeline.get_clock().get_time() - recorder.pipeline.get_base_time()
            assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK
            assert handled.wait(5)  # Bound only: the retired branch's sample failed.
            recorder._executor.submit(lambda: None).result(5)  # Bound only: any refusal it queued ran.
            ran = threading.Event()
            GLib.idle_add(ran.set)
            spin_until(ran.is_set)  # And any finish it queued.

        late_failure()
        assert recorder._pending_sources.get("system") is new and not new["error"] and not new["retired"]
        assert "refusals" not in session.manifest and not recorder._finishing
        sides.fire("system")
        spin_until(lambda: recorder._track_ids.get("system") is not None)
        late_failure()
        assert not recorder._finishing and "error" not in names(signals)
        stop(recorder, signals)
    finally:
        old["branch"].set_state(Gst.State.NULL)
        close(recorder)
    stored = saved(session)
    assert stored["state"] == "completed" and "refusals" not in stored and "problem" not in stored
    assert [(t["kind"], t["frames"]) for t in stored["tracks"]] == [("microphone", 480), ("system", 480)]


def test_an_armed_turn_on_refused_by_an_explicit_error_is_refused_at_its_time(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        session = recorder.session
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)
        recorder._elapsed_ms = lambda _pipeline: 7000
        turn_on(recorder, True, True)  # Try again: armed, the start refusal stays.
        assert recorder._pending_sources["system"]["armed"]
        post_error(sides, "system", DENIED)
        spin_until(lambda: len(session.manifest["refusals"]) == 2)
        assert [(r["atMs"], r["code"]) for r in session.manifest["refusals"]] == [
            (0, "deviceUnavailable"),
            (7000, "permissionDenied"),
        ]
        assert recorder.refusals[-1]["atMs"] == 0  # The start's list is unchanged.
        assert not recorder._finishing
        stop(recorder, signals)
    finally:
        close(recorder)
    assert saved(session)["state"] == "completed"


# The browser path's system tap (BrowserCapture with others from computer sound).


class QuietTaps:
    """A tap factory: the microphone delivers at once, the system tap only when told."""

    def __init__(self):
        self.made = []

    def __call__(self, kind, origin, append, failed, *, level):
        taps = self

        class Tap:
            pipeline = None
            refusal_code = "deviceUnavailable"

            def __init__(self):
                self.kind, self.append, self.failed = kind, append, failed
                self.stopped = threading.Event()

            def start(self):
                if kind == "microphone":
                    self.append(struct.pack("<h", 1) * 480, 0)  # Its first buffer.

            def stop(self):
                self.stopped.set()

        tap = Tap()
        taps.made.append(tap)
        return tap

    def of(self, kind):
        return [tap for tap in self.made if tap.kind == kind]


def test_a_silent_system_tap_commits_armed_and_records_from_its_late_first_buffer(tmp_path):
    taps = QuietTaps()
    capture, result, changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        assert result["type"] == "started" and not capture.finished and "error" not in changes
        assert set(capture.native_taps) == {"microphone", "system"} and kinds_of(session) == ["microphone"]
        assert "refusals" not in session.manifest and not capture.refused_sides
        assert live(session) == []
        at = capture.elapsed_ms() + 10500
        (system,) = taps.of("system")
        system.append(struct.pack("<h", 7) * 480, at)
        assert kinds_of(session) == ["microphone", "system"]
        track = session.manifest["tracks"][1]
        assert track["startOffsetMs"] == pytest.approx(at) and track["frames"] == 480
        assert track["file"] == "track-system.wav" and "sources" in changes
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed" and "refusals" not in session.manifest


def test_a_system_tap_silent_until_stop_leaves_no_track_and_no_refusal(tmp_path):
    taps = QuietTaps()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    native_pcm(capture, "microphone")
    capture.stop()
    assert kinds_of(session) == ["microphone"] and "refusals" not in session.manifest
    assert session.manifest["state"] == "completed" and "error" not in changes
    assert taps.of("system")[0].stopped.is_set()


@pytest.mark.parametrize("code", ["permissionDenied", "deviceUnavailable"])
def test_an_armed_system_tap_refused_by_an_explicit_error_keeps_the_recording(tmp_path, code):
    taps = QuietTaps()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        (system,) = taps.of("system")
        system.refusal_code = code
        system.failed("Fake monitor denied")
        assert session.manifest["refusals"] == [dict(atMs=0, side="others", code=code, message="Fake monitor denied")]
        assert "system" not in capture.native_taps and capture.refused_sides == {"system"}
        assert not capture.error and "error" not in changes and not capture.finished
        assert system.stopped.wait(5)  # Bound only: stopped off the tap's own thread.
        native_pcm(capture, "microphone")
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed"


def test_a_silent_system_turn_on_is_armed_and_refused_only_at_its_time(tmp_path):
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        capture.select(True, False)
        capture.select(True, True)
        turned_on = [e for e in session.manifest["events"] if e["type"] == "selection"][-1]["atMs"]
        assert kinds_of(session) == ["microphone"] and "system" in capture.native_taps
        taps.of("system")[-1].failed("Fake monitor went away")
        assert session.manifest["refusals"] == [
            dict(atMs=turned_on, side="others", code="deviceUnavailable", message="Fake monitor went away")
        ]
    finally:
        capture.stop()


class StopBlocks:
    """A tap's stop() that blocks until the test releases it, as a device that
    still delivers while it shuts down."""

    def __init__(self, tap):
        self.entered, self.release = threading.Event(), threading.Event()
        tap.stop = self

    def __call__(self):
        self.entered.set()
        assert self.release.wait(5), "Test did not release the tap's stop"  # Bound only.


@pytest.mark.parametrize("late", [("buffer",), ("error",), ("buffer", "error")])
def test_browser_an_armed_tap_deselected_while_it_delivers_records_nothing_after(tmp_path, late):
    taps = QuietTaps()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    try:
        (system,) = taps.of("system")
        stopping = StopBlocks(system)
        done = []
        worker = threading.Thread(target=lambda: done.append(capture.select(True, False)), daemon=True)
        worker.start()
        assert stopping.entered.wait(5)  # Bound only: the deselect committed and now stops the tap.
        # While the tap stops, its device delivers its first buffer and/or fails.
        if "buffer" in late:
            system.append(struct.pack("<h", 7) * 480, capture.elapsed_ms())
        if "error" in late:
            system.failed("Fake monitor went away")
        stopping.release.set()
        worker.join(5)
        assert done and not worker.is_alive()
        assert kinds_of(session) == ["microphone"] and "system" not in capture.native_taps
        assert not any(identifier.startswith("system") for identifier in session.writers)
        selection = [e for e in session.manifest["events"] if e["type"] == "selection"][-1]
        assert (selection["me"], selection["others"]) == (True, False)
        assert "refusals" not in session.manifest and not capture.refused_sides
        assert not capture.error and "error" not in changes and capture.snapshot()["armed"] == {}
        native_pcm(capture, "microphone")
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed" and "refusals" not in session.manifest
    assert [(t["kind"], t["frames"]) for t in session.manifest["tracks"]] == [("microphone", 480)]


def test_browser_a_retired_armed_tap_never_feeds_the_next_turn_on(tmp_path):
    taps = QuietTaps()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    try:
        (first,) = taps.of("system")
        capture.select(True, False)
        capture.select(True, True)
        second = taps.of("system")[-1]
        armed = capture.snapshot()["armed"]
        assert second is not first and set(armed) == {"system"} and armed["system"] > 0
        # The turned-off tap's thread delivers late, after the turn-on armed another.
        first.append(struct.pack("<h", 7) * 480, capture.elapsed_ms())
        first.failed("Fake monitor went away")
        assert kinds_of(session) == ["microphone"] and "refusals" not in session.manifest
        assert capture.snapshot()["armed"] == armed and not capture.error and "error" not in changes
        # The new tap's own first buffer opens the side; the old tap's never reaches it.
        second.append(struct.pack("<h", 7) * 480, armed["system"] + 10)
        first.append(struct.pack("<h", 7) * 480, armed["system"] + 20)
        assert kinds_of(session) == ["microphone", "system"]
        assert session.manifest["tracks"][1]["frames"] == 480 and capture.snapshot()["armed"] == {}
    finally:
        capture.stop()
    assert session.manifest["state"] == "completed" and "refusals" not in session.manifest


# The real tap: no first buffer is not a failure for the system side.


class Devices(list):
    """The silent devices made; `deny` posts a denied access as each is made."""

    deny = False


@pytest.fixture
def silent_device(monkeypatch):
    """NativeTap's test source replaced by a silent appsrc, and a short window."""
    from cassini_talk import native_tap

    parse = Gst.parse_launch
    made = Devices()

    def launch(description):
        description = description.replace(
            description.split(" ! ", 1)[0],
            "appsrc name=device is-live=true format=time do-timestamp=true "
            'caps="audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved"',
        )
        pipeline = parse(description)
        made.append(pipeline)
        if made.deny:
            # Queued on the bus before the tap reads it: within its window.
            device = pipeline.get_by_name("device")
            error = GLib.Error.new_literal(Gst.ResourceError.quark(), "Access denied", int(DENIED))
            device.post_message(Gst.Message.new_error(device, error, "scripted"))
        return pipeline

    monkeypatch.setattr(native_tap.Gst, "parse_launch", launch)
    monkeypatch.setattr(native_tap, "OPEN_TIMEOUT_S", 0.2, raising=False)
    return made


def test_a_silent_system_tap_starts_armed_and_stops_without_waiting(silent_device):
    from cassini_talk.native_tap import NativeTap
    from cassini_talk.session_clock import SessionClock

    received, failures = [], []
    tap = NativeTap(
        "system", SessionClock(0), lambda data, *_a, **_k: received.append(data), failures.append, test=True
    )
    tap.start()  # Returns after its window: armed, not refused.
    assert not tap.heard.is_set() and tap.error is None
    device = silent_device[0].get_by_name("device")
    assert device.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(960))) == Gst.FlowReturn.OK
    assert tap.heard.wait(5)  # Bound only: the late first buffer reaches append.
    assert len(received) == 1 and not failures
    tap.stop()


def test_a_never_heard_system_tap_stops_at_once(silent_device):
    from cassini_talk.native_tap import NativeTap
    from cassini_talk.session_clock import SessionClock

    tap = NativeTap("system", SessionClock(0), lambda *_a, **_k: None, lambda _m: None, test=True)
    tap.start()
    # Like a Pulse read on a suspended monitor, this device never lets an EOS through.
    pad = silent_device[0].get_by_name("device").get_static_pad("src")
    pad.add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM,
        lambda _pad, info: (
            Gst.PadProbeReturn.DROP if info.get_event().type == Gst.EventType.EOS else Gst.PadProbeReturn.OK
        ),
    )
    began = time.monotonic()
    tap.stop()  # No EOS to drain from a device that never delivered: no 10 s timeout.
    assert time.monotonic() - began < 5


def test_a_tap_stop_sealed_drops_what_its_device_delivers_after_stop(silent_device):
    """R93-2: Stop seals a tap that never received a buffer, and releases it at
    once. Its device can still deliver before the teardown: that buffer was
    captured after Stop and never enters the tap's queue (BrowserCapture's cut
    at the Stop moment is the other line of defence)."""
    from cassini_talk.native_tap import NativeTap
    from cassini_talk.session_clock import SessionClock

    received, failures = [], []
    tap = NativeTap(
        "system", SessionClock(0), lambda data, *_a, **_k: received.append(data), failures.append, test=True
    )
    tap.start()  # Armed: nothing delivered yet.
    device = silent_device[0].get_by_name("device")
    left, seen = threading.Condition(), []

    def leaving(_pad, info):
        with left:
            seen.append("buffer" if info.type & Gst.PadProbeType.BUFFER else info.get_event().type)
            left.notify_all()
        return Gst.PadProbeReturn.OK

    queue = tap.pipeline.get_by_name("tap-queue").get_static_pad("src")
    queue.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, leaving)

    class Late(threading.Event):
        """Set by Stop once it sealed the tap, before its teardown: the device
        delivers then, and its end of stream marks that the buffer went by."""

        delivered = False

        def set(self):
            if not self.delivered:
                self.delivered = True
                assert device.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(960))) == Gst.FlowReturn.OK
                assert device.emit("end-of-stream") == Gst.FlowReturn.OK
                with left:
                    assert left.wait_for(lambda: Gst.EventType.EOS in seen, BOUND)
            super().set()

    tap.ended = Late()
    tap.stop()
    assert tap.ended.delivered and "buffer" not in seen and not received and not failures, (seen, len(received))


class Signalled(threading.Event):
    """An Event that also wakes `condition` when it is set."""

    def __init__(self, condition):
        super().__init__()
        self.condition = condition

    def set(self):
        super().set()
        with self.condition:
            self.condition.notify_all()


def test_an_armed_tap_whose_first_buffer_waits_behind_the_capture_lock_drains_at_stop(silent_device):
    """R93-2 (#230 review): heard is set only once the first buffer's append
    returns. When the armed tap's first callback waits behind the capture lock
    (a stalled checkpoint) as Stop comes, it and the buffers queued behind it
    were captured before Stop: Stop drains them through EOS, instead of tearing
    the tap down as one that never heard a buffer and flushing its queue."""
    from cassini_talk.native_tap import NativeTap
    from cassini_talk.session_clock import SessionClock

    decided = threading.Condition()
    entered, release = threading.Event(), threading.Event()
    queued, received, failures = [], [], []

    def append(data, offset, **_):
        if not received and not entered.is_set():
            entered.set()
            assert release.wait(BOUND)  # The capture lock, held by a stalled checkpoint.
        received.append(data)

    tap = NativeTap("system", SessionClock(0), append, failures.append, test=True)
    tap.ended = Signalled(decided)  # Set at once by a Stop that skips the drain.
    tap.start()  # Armed: the silent device has delivered nothing yet.
    queue = tap.pipeline.get_by_name("tap-queue")

    def entering(_pad, info):
        with decided:
            if info.type & Gst.PadProbeType.BUFFER:
                queued.append(info.get_buffer().get_size())
            elif info.get_event().type == Gst.EventType.EOS:
                queued.append("EOS")  # The drain began.
            decided.notify_all()
        return Gst.PadProbeReturn.OK

    queue.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, entering)
    device = silent_device[0].get_by_name("device")
    assert device.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(960))) == Gst.FlowReturn.OK
    assert entered.wait(BOUND)
    assert not tap.heard.is_set()  # Its first buffer is not written yet.
    for _ in range(9):  # Queued behind it, all captured before Stop.
        assert device.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(960))) == Gst.FlowReturn.OK
    with decided:
        assert decided.wait_for(lambda: len(queued) == 10, BOUND)
    result = {}

    def stop():
        try:
            tap.stop()
        except BaseException as error:  # pragma: no cover - asserted below
            result["error"] = error

    stopping = threading.Thread(target=stop, name="test-tap-stop")
    stopping.start()
    with decided:  # Stop decides: drain (EOS reaches the queue) or skip (ended at once).
        assert decided.wait_for(lambda: "EOS" in queued or tap.ended.is_set(), BOUND)
        drained = "EOS" in queued
    release.set()  # The stall ends.
    stopping.join(BOUND)
    assert not stopping.is_alive() and "error" not in result, result
    # Skipping the drain tears the pipeline down and flushes the queue: how much
    # of it still slips through before the flush is a race, so the decision is checked.
    assert drained, "Stop skipped the drain of a tap with pre-Stop input queued"
    assert len(received) == 10 and not failures, (len(received), failures)
    assert b"".join(received) == bytes(10 * 960)


def stop_with_an_armed_buffer_on_its_way(tmp_path, monkeypatch, plan, captured, order):
    """The native-only recorder, its others' side armed (silent so far), on
    branches with the real tap queue. 10 ms frames of computer sound reach the
    others' queue, and the first one is still on its way to the writer as Stop
    is pressed. `captured` places them: "before" Stop (100 ms ending as Stop is
    pressed) or "after" it (one frame stamped 5 s on). `order` decides which
    runs first once the first frame and Stop meet, both being possible:
    "stop-first": Stop's release of the armed sides (the frame is held between
    the queue and the writer meanwhile); "callback-first": the frame's callback,
    which waited behind the PCM lock (a checkpoint stalled on storage) while
    Stop's release waits behind other IO work. Returns (recorder, signals,
    session, first frame's ms)."""
    from cassini_talk.recorder import TAP_QUEUE

    parse = Gst.parse_bin_from_description
    monkeypatch.setattr(
        Gst,
        "parse_bin_from_description",
        lambda description, ghost: parse(description.replace(" ! appsink", f" ! {TAP_QUEUE} ! appsink"), ghost),
    )
    recorder, sides, signals = start(tmp_path, plan)
    session, pipeline = recorder.session, recorder.pipeline
    holder, release, gate = None, threading.Event(), threading.Event()
    try:
        attempt = recorder._pending_sources["system"]
        assert attempt["armed"] and attempt["branch"].get_by_name("tap-queue") is not None
        clock, base = pipeline.get_clock(), pipeline.get_base_time()
        count = 10 if captured == "before" else 1
        # The frames end at, or start after, Stop's moment: the pipeline runs
        # their length first (a clock wait for that running time, no sleep).
        Gst.Clock.id_wait(clock.new_single_shot_id(base + count * FRAME_NS))
        reached, admitted = threading.Event(), threading.Event()

        def entering(*_):
            if not reached.is_set():
                reached.set()
                if order == "stop-first":
                    assert release.wait(3 * BOUND)  # Bound only: the test releases it.
            return Gst.PadProbeReturn.OK

        sink = attempt["branch"].get_by_name("system").get_static_pad("sink")
        sink.add_probe(Gst.PadProbeType.BUFFER, entering)
        admit = recorder._admit

        def admitting(*args):
            try:
                return admit(*args)
            finally:
                admitted.set()

        recorder._admit = admitting
        if order == "callback-first":
            held = threading.Event()

            def hold():
                with recorder._lock:
                    held.set()
                    assert release.wait(3 * BOUND)  # Bound only: the test releases it.

            holder = threading.Thread(target=hold, name="test-stall")
            holder.start()
            assert held.wait(BOUND)
            recorder._executor.submit(gate.wait, 3 * BOUND)  # Other IO work ahead of Stop's release.
        now = clock.get_time() - base
        first = now - count * FRAME_NS if captured == "before" else now + 5 * Gst.SECOND
        for index in range(count):
            buffer = Gst.Buffer.new_wrapped(FRAME)
            buffer.pts, buffer.duration = first + index * FRAME_NS, FRAME_NS
            assert sides.sources["system"].emit("push-buffer", buffer) == Gst.FlowReturn.OK
        assert reached.wait(BOUND)  # The first frame is on its way to the writer.
        assert "system" in recorder._pending_sources and not admitted.is_set()
        recorder.stop()
        assert recorder._stop_at_ms >= now / Gst.MSECOND
        if order == "stop-first":
            spin_until(lambda: attempt["retired"] or attempt.get("draining"))
            release.set()  # The frame goes on to the writer.
        else:
            release.set()  # The stall ends; the frame's callback takes the lock.
            holder.join(BOUND)
            assert admitted.wait(BOUND)
            gate.set()
        dispatch_until(
            lambda: "finished" in names(signals) and recorder.pipeline is None and not recorder._finishing,
            "the armed-side capture did not finish",
        )
    finally:
        release.set()
        gate.set()
        if holder is not None:
            holder.join(BOUND)
        close(recorder)
    return recorder, signals, session, first / Gst.MSECOND


ORDERS = pytest.mark.parametrize("order", ["stop-first", "callback-first"])


@ORDERS
@pytest.mark.parametrize("microphone", ["ok", "fail"])
def test_native_an_armed_side_whose_first_buffer_is_on_its_way_at_stop_records_it(
    tmp_path, monkeypatch, microphone, order
):
    """R93-2 (#230 review): the native-only recorder. The others' side heard its
    first buffers before Stop, but they were still queued, the first one on its
    way to the writer (or waiting behind the PCM lock), as Stop was pressed.
    They were heard before Stop: Stop drains them into a track that opens at the
    first and ends at Stop's moment (D4), instead of releasing the side as one
    that never heard a buffer. With the microphone refused, they keep the folder."""
    plan = {"microphone": ("ok", 0.1) if microphone == "ok" else ("fail", 0), "system": ("never", 0)}
    recorder, signals, session, first = stop_with_an_armed_buffer_on_its_way(
        tmp_path, monkeypatch, plan, "before", order
    )
    assert names(signals) == ["started", "finished"], signals
    assert not recorder.discarded and recorder.unheard == set()
    stored = saved(session)
    assert stored["state"] == "completed" and "problem" not in stored
    assert [t["kind"] for t in stored["tracks"]] == (["microphone"] if microphone == "ok" else []) + ["system"]
    system = stored["tracks"][-1]
    # Every frame heard before Stop is kept, from the first one on.
    assert system["frames"] == 10 * 480 and system["startOffsetMs"] == pytest.approx(first)
    assert system["endOffsetMs"] == pytest.approx(first + 100) and system["endOffsetMs"] <= recorder._stop_at_ms
    end = stored["events"][-1]
    assert end["type"] == "end" and end["reason"] == "user"
    assert end["atMs"] == pytest.approx(max(recorder._stop_at_ms, *(t["endOffsetMs"] for t in stored["tracks"])))


@ORDERS
def test_native_an_armed_side_heard_only_after_stop_records_nothing_and_never_waits_out_the_drain(
    tmp_path, monkeypatch, order
):
    """Its only frame reached the queue before Stop's release but was captured
    after Stop's moment: the side drains (its EOS is not held back, so the
    recording does not wait out the 10 s bound) and opens no track. With the
    microphone refused nothing opened: no folder, the start banner (D58 point 2)."""
    plan = {"microphone": ("fail", 0), "system": ("never", 0)}
    recorder, signals, session, _first = stop_with_an_armed_buffer_on_its_way(
        tmp_path, monkeypatch, plan, "after", order
    )
    assert recorder.discarded and not session.directory.exists() and not list(tmp_path.iterdir())
    assert names(signals) == ["started", "error", "finished"] and signals[-1] == ("finished", "")
    assert signals[1][1] != "Timed out while saving the recording."
    assert [(r["side"], r["code"]) for r in R.banner(recorder.refusals)["refusals"]] == [("me", "deviceUnavailable")]
    assert recorder.unheard == {"system"}


@pytest.mark.parametrize("kind", ["microphone", "system"])
def test_a_silent_tap_window(silent_device, kind):
    from cassini_talk.native_tap import NativeTap
    from cassini_talk.session_clock import SessionClock

    tap = NativeTap(kind, SessionClock(0), lambda *_a, **_k: None, lambda _m: None, test=True)
    if kind == "microphone":
        with pytest.raises(OSError, match="did not produce a sample"):
            tap.start()  # The microphone keeps its bound.
    else:
        tap.start()
        tap.stop()


def test_an_explicit_tap_error_carries_its_code(silent_device):
    from cassini_talk.native_tap import CaptureError, NativeTap
    from cassini_talk.session_clock import SessionClock

    silent_device.deny = True
    tap = NativeTap("system", SessionClock(0), lambda *_a, **_k: None, lambda _m: None, test=True)
    with pytest.raises(CaptureError) as raised:
        tap.start()
    assert raised.value.refusal_code == "permissionDenied" and str(raised.value) == "Access denied"


def test_the_sound_check_reads_a_never_heard_others_side_as_silent():
    from cassini_talk.refusals import entry
    from cassini_talk.sound_check import REFUSED, SILENT, Outcome, judge_streams

    mic = entry(0, "me", "deviceUnavailable", message="No such entity")
    outcomes = judge_streams(None, [], [mic], {"system"})
    assert outcomes == dict(microphone=Outcome(REFUSED, mic), system=Outcome(SILENT))
    # Without that fact, a side that never opened is still refused.
    assert judge_streams(None, [], [mic])["system"].state == REFUSED


# Waiting for sound shows no refusal (D58.1): the recording page's lines read the
# live armed sides, which the folder cannot express (Try again writes no event).


def shown(recorder):
    """The recording page's lines for a native recording, as the window reads them."""
    manifest = recorder.session.manifest
    tracks = [(t["kind"], t.get("ended"), t.get("startOffsetMs")) for t in manifest["tracks"]]
    return R.live_lines(manifest.get("refusals"), tracks, manifest.get("events") or (), recorder.armed_sides())


def browser_shown(capture):
    """The same for a browser capture: its folder plus the armed sides of its status."""
    manifest = capture.host.session.manifest
    tracks = [(t["kind"], t.get("ended"), t.get("startOffsetMs")) for t in manifest["tracks"]]
    return R.live_lines(manifest.get("refusals"), tracks, manifest.get("events") or (), capture.snapshot()["armed"])


OTHERS_LINE = dict(side="others", id="errors.deviceUnavailable.others", caption=None)


def test_an_armed_side_covers_only_the_refusals_before_its_attempt():
    stored = [R.entry(0, "others", "deviceUnavailable")]
    assert R.live_lines(stored, []) == [OTHERS_LINE]
    assert R.live_lines(stored, [], (), {"system": 0}) == []
    assert R.live_lines(stored, [], (), {"system": 7000}) == []
    later = [R.entry(7000, "others", "permissionDenied")]
    assert R.live_lines(later, [], (), {"system": 3000}) == [OTHERS_LINE]
    # Not the others' kind, or unreadable: no cover.
    assert R.live_lines(stored, [], (), {"microphone": 9000}) == [OTHERS_LINE]
    assert R.live_lines(stored, [], (), {"system": "9000"}) == [OTHERS_LINE]
    assert R.live_lines(stored, [], (), [("system", 9000)]) == [OTHERS_LINE]
    # Computer sound selected and waiting records the others: the fallback line.
    browser = [R.entry(0, "browser", "startRejected", detail="noSources")]
    assert [line["id"] for line in R.live_lines(browser, [], (), {"system": 0})] == ["errors.othersFallback.line"]
    assert R.live_lines(browser, []) == []


def test_try_again_on_refused_others_that_stay_silent_shows_no_refusal(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        session = recorder.session
        assert shown(recorder) == [OTHERS_LINE]
        recorder._turn_on_wait = quiet_turn_on
        sides.plan["system"] = ("never", 0)
        recorder._elapsed_ms = lambda _pipeline: 7000
        turn_on(recorder, True, True)  # Try again: silent, so armed.
        assert recorder.armed_sides() == {"system": 7000}
        assert shown(recorder) == []
        # A later explicit error is its own refusal, at the attempt's time.
        post_error(sides, "system", DENIED)
        spin_until(lambda: len(session.manifest.get("refusals", ())) == 2)
        spin_until(lambda: recorder.armed_sides() == {})
        assert shown(recorder) == [OTHERS_LINE]
        assert session.manifest["refusals"][-1]["atMs"] == 7000
        stop(recorder, signals)
    finally:
        close(recorder)


def test_refused_others_turned_off_then_on_and_silent_show_no_refusal(tmp_path, quiet_turn_on):
    recorder, sides, signals = start(tmp_path, {"microphone": ("ok", 0.1), "system": ("fail", 0)})
    try:
        recorder._turn_on_wait = quiet_turn_on
        recorder._elapsed_ms = lambda _pipeline: 4000
        turn_on(recorder, True, False)
        assert shown(recorder) == []
        recorder._elapsed_ms = lambda _pipeline: 8000
        sides.plan["system"] = ("never", 0)
        turn_on(recorder, True, True)
        assert recorder.armed_sides() == {"system": 8000} and shown(recorder) == []
        stop(recorder, signals)
    finally:
        close(recorder)


def test_the_page_reads_armed_sides_while_stop_holds_the_recorder_lock(tmp_path):
    # Boards' quit stop-finishing: the GTK main loop asked for armed_sides while
    # Stop held the recorder lock, and the page stalled until the session was
    # left recording.
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        read = threading.Event()
        found = []
        with recorder._lock:
            worker = threading.Thread(target=lambda: (found.append(recorder.armed_sides()), read.set()), daemon=True)
            worker.start()
            assert read.wait(5), "armed_sides waited on the recorder lock"
        assert found == [{"system": 0}]
        stop(recorder, signals)
    finally:
        close(recorder)


def test_a_silent_start_is_armed_at_zero_and_shows_no_line(tmp_path):
    recorder, sides, signals = start(tmp_path, QUIET)
    try:
        assert recorder.armed_sides() == {"system": 0} and shown(recorder) == []
        sides.now = BOUND + 5
        sides.fire("system")
        spin_until(lambda: "system" in recorder._track_ids)
        assert recorder.armed_sides() == {}
        stop(recorder, signals)
    finally:
        close(recorder)


def test_browser_try_again_on_a_refused_system_tap_that_stays_silent_shows_no_refusal(tmp_path):
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    try:
        session = capture.host.session
        assert capture.snapshot()["armed"] == {"system": 0}
        (system,) = taps.of("system")
        system.failed("Fake monitor went away")  # An explicit error: refused at 0.
        assert capture.snapshot()["armed"] == {} and browser_shown(capture) == [OTHERS_LINE]
        capture.select(True, True, retry=True)  # Try again: silent, so armed.
        armed = capture.snapshot()["armed"]
        assert set(armed) == {"system"} and armed["system"] > 0 and browser_shown(capture) == []
        # Its first buffer gives it a track; it is no longer armed.
        taps.of("system")[-1].append(struct.pack("<h", 7) * 480, armed["system"] + 10)
        assert kinds_of(session) == ["microphone", "system"] and capture.snapshot()["armed"] == {}
        assert browser_shown(capture) == []
    finally:
        capture.stop()


def test_the_window_reads_a_browser_capture_s_armed_sides_from_its_status(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:
        pytest.skip("Adwaita is unavailable")
    from cassini_talk.library_window import LibraryWindow

    identifier = str(uuid4())
    directory = tmp_path / f"Recording-{identifier}"
    status = dict(path=str(directory), recording=True, armed={"system": 1200})
    app = SimpleNamespace(_browser_snapshot=dict(statuses=[dict(path=str(tmp_path / "Recording-x"), armed={})]))
    window = SimpleNamespace(_browser_snapshot=dict(statuses=[status]), get_application=lambda: app)
    meeting = SimpleNamespace(id=identifier, directory=directory)
    assert LibraryWindow._armed_sides(window, meeting) == {"system": 1200}
    window._browser_snapshot = {}
    assert LibraryWindow._armed_sides(window, meeting) is None
    app._browser_snapshot = dict(statuses=[status])
    assert LibraryWindow._armed_sides(window, meeting) == {"system": 1200}
    assert LibraryWindow._armed_sides(window, SimpleNamespace(id=str(uuid4()), directory=directory)) is None


# The browser path ends like the native one when no side ever opened (D58 point 2).


class RefusingMicrophone(QuietTaps):
    """The microphone tap reports an explicit error at start; the system tap is quiet."""

    def __call__(self, kind, origin, append, failed, *, level):
        tap = super().__call__(kind, origin, append, failed, level=level)
        if kind == "microphone":

            def start():
                raise OSError("No such entity")

            tap.start = start
        return tap


def test_browser_refused_microphone_and_others_silent_until_stop_leave_no_folder(tmp_path):
    taps = RefusingMicrophone()
    capture, result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    try:
        assert result["type"] == "started" and capture.snapshot()["armed"] == {"system": 0}
        assert [(r["side"], r["code"]) for r in session.manifest["refusals"]] == [("me", "deviceUnavailable")]
        assert [line["side"] for line in browser_shown(capture)] == ["me"]
    finally:
        stopped = capture.stop()
    # Nothing ever opened: no folder, never the "empty" problem.
    assert stopped == dict(type="stopped", path=None) and capture.discarded and capture.nothing_recorded
    assert not session.directory.exists() and "error" not in changes


def test_browser_refused_microphone_and_a_late_others_error_end_with_no_folder(tmp_path):
    taps = RefusingMicrophone()
    capture, _result, changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    try:
        (system,) = taps.of("system")
        system.refusal_code = "permissionDenied"
        system.failed("Fake monitor denied")
        assert [(r["side"], r["code"]) for r in session.manifest["refusals"]] == [
            ("me", "deviceUnavailable"),
            ("others", "permissionDenied"),
        ]
        # Nothing is left that could open: the capture asks to end (as the service does on "error").
        assert "error" in changes and capture.error == "Fake monitor denied"
    finally:
        stopped = capture.stop(capture.error)
    assert stopped == dict(type="stopped", path=None) and capture.discarded and not session.directory.exists()


def test_browser_silent_others_with_a_recorded_microphone_keep_the_folder(tmp_path):
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    native_pcm(capture, "microphone")
    assert capture.stop()["path"] == str(session.directory) and not capture.discarded
    assert session.manifest["state"] == "completed" and session.manifest.get("problem") is None


def test_browser_an_opened_microphone_keeps_the_folder_before_its_first_recorded_frame(tmp_path):
    """Opened is a committed track (as natively), not a frame count: no discard."""
    taps = QuietTaps()
    capture, _result, _changes = browser_start(tmp_path, taps, others_source="system")
    session = capture.host.session
    assert kinds_of(session) == ["microphone"] and session.manifest["tracks"][0]["frames"] == 0
    assert capture.stop()["path"] == str(session.directory) and not capture.discarded
    assert session.directory.exists()
