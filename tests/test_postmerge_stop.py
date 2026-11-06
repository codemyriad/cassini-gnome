# SPDX-License-Identifier: GPL-3.0-or-later
"""Post-merge Stop review: order real saves with events, never delay guesses."""

import json
import queue
import threading

import pytest

pytest.importorskip("gi")
from test_tap_stall import (  # noqa: E402
    BOUND,
    DrainingTaps,
    OrderedLock,
    drain_across_stop,
    guest_audio,
    in_thread,
    record_live,
    stall_holding,
    start_capture,
)

from cassini_talk import native_recorder  # noqa: E402
from cassini_talk.browser_service import BrowserConnection, BrowserService  # noqa: E402
from cassini_talk.recording_session import ProtocolError, RecordingSession  # noqa: E402


def broker(tmp_path, capture):
    service = BrowserService(tmp_path / "config.json", notifier=lambda *_: None)
    service.captures[capture.capture_id] = capture
    identity = ("browser-instance", 3, "https://meet.example/call")
    service.call_captures[identity[:2]] = (capture.capture_id, identity[2])
    finished = threading.Event()

    def changed(owner, event):
        service._capture_changed(owner, event)
        if event == "finished":
            finished.set()

    capture.changed = changed
    return service, identity, finished


@pytest.mark.parametrize(
    "route,first,later",
    [("browser", "user", "callEnded"), ("cancel", "user", "callEnded"), ("browser", "callEnded", "user")],
)
def test_first_admitted_stop_owns_reason_problem_and_auto_restart_suppression(tmp_path, route, first, later):
    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    tap, start = record_live(capture, clock)
    service, identity, finished = broker(tmp_path, capture)
    go = threading.Event()
    ordered = capture.lock = OrderedLock(capture.lock, "stop-a", go)
    holder, release = stall_holding(capture)
    threads = []
    try:
        a, result_a = in_thread(
            "stop-a",
            lambda: capture.handle(dict(type="stop", captureId="stall", reason=first, problem="First Stop problem.")),
        )
        threads.append(a)
        assert ordered.waited_by("stop-a") and capture.stop_requested.is_set()
        clock.ms = start + 1500
        if route == "browser":
            b, result_b = in_thread(
                "stop-b", lambda: capture.handle(dict(type="stop", captureId="stall", reason=later))
            )
            threads.append(b)
            assert ordered.waited_by("stop-b")
        else:
            with service.lock:
                service._cancel_capture("stall", identity, later)
        release.set()
        holder.join(BOUND)
        assert tap.draining.wait(BOUND)
        drain_across_stop(tap, start, clock)
        assert finished.wait(BOUND)
        saved = json.loads((capture.directory / "session.json").read_text())
        actual = dict(
            reason=saved["events"][-1]["reason"],
            problem=saved.get("problem"),
            user=getattr(capture, "user_stopped", False),
            suppressed=service._stopped_identity(identity),
        )
        assert actual == dict(
            reason=first, problem="First Stop problem.", user=first == "user", suppressed=first == "user"
        )
    finally:
        release.set()
        go.set()
        tap.drained.set()
        holder.join(BOUND)
        for thread in threads:
            thread.join(BOUND)
        service.workers.shutdown(wait=True)
    assert not any(thread.is_alive() for thread in threads)
    assert "error" not in result_a, result_a
    if route == "browser":
        assert "error" not in result_b, result_b


def test_invalid_stop_problem_is_refused_before_sealing_or_stopping_devices(tmp_path):
    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    tap, _ = record_live(capture, clock)
    tap.drained.set()
    try:
        with pytest.raises(ProtocolError, match="string"):
            capture.handle(dict(type="stop", captureId="stall", problem=123))
        assert not capture.stop_requested.is_set()
        assert not capture.host.session.capture_record.sealed
        assert not tap.draining.is_set() and not capture.finished and not capture.host.session.finished
    finally:
        capture.stop()
        if not capture.host.session.finished:  # Release the original bug's unfinished writer too.
            capture.host.session.finish()


def test_lost_packet_superseded_by_call_end_cannot_become_a_user_stop(tmp_path):
    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    tap, start = record_live(capture, clock)
    service, identity, finished = broker(tmp_path, capture)
    go, joined = threading.Event(), threading.Event()
    ordered = capture.lock = OrderedLock(capture.lock, "lost-packet", go)
    holder, release = stall_holding(capture)
    stop = capture.stop

    def observed_stop(*args, **kwargs):
        if threading.current_thread().name == "lost-packet":
            joined.set()
        return stop(*args, **kwargs)

    capture.stop = observed_stop
    lost, result = in_thread(
        "lost-packet",
        lambda: capture.handle(
            dict(type="stop", captureId="stall", failureReason="lost", problem="Superseded transport loss.")
        ),
    )
    try:
        assert ordered.waited_by("lost-packet") and not capture.stop_requested.is_set()
        with service.lock:
            service._cancel_capture("stall", identity, "callEnded")
        release.set()
        holder.join(BOUND)
        assert tap.draining.wait(BOUND)
        go.set()
        assert joined.wait(BOUND)
        drain_across_stop(tap, start, clock)
        assert finished.wait(BOUND)
        saved = json.loads((capture.directory / "session.json").read_text())
        assert (
            saved["events"][-1]["reason"],
            saved.get("problem"),
            getattr(capture, "user_stopped", False),
            service._stopped_identity(identity),
        ) == ("callEnded", None, False, False)
    finally:
        release.set()
        go.set()
        tap.drained.set()
        holder.join(BOUND)
        lost.join(BOUND)
        service.workers.shutdown(wait=True)
    assert not lost.is_alive() and "error" not in result, result


@pytest.mark.parametrize("already_seen", [False, True])
def test_later_nonempty_stop_cannot_rewrite_the_sealed_source_ledger(tmp_path, already_seen):
    taps = DrainingTaps()
    capture, clock = start_capture(
        tmp_path, taps, microphone=True, others_source="browser", features=("source-outcomes-v1",)
    )
    tap, start = record_live(capture, clock)
    source = dict(sourceTrackId="x", kind="remote")
    if already_seen:
        assert (
            capture.handle(
                dict(type="track", id="guest", sourceTrackId="x", kind="remote", sampleRate=48000, channels=1)
            )["type"]
            == "ack"
        )
        assert capture.handle(guest_audio(0, 48000, 0))["type"] == "ack"
    a, result_a = in_thread(
        "stop-a", lambda: capture.handle(dict(type="stop", captureId="stall", discoveredSources=[source]))
    )
    assert tap.draining.wait(BOUND)  # A merged its summary and released the writer lock to drain.
    processed = threading.Event()
    stop = capture.stop

    def observed_stop(*args, **kwargs):
        if threading.current_thread().name == "stop-b":
            processed.set()
        return stop(*args, **kwargs)

    capture.stop = observed_stop

    def second():
        try:
            return capture.handle(dict(type="stop", captureId="stall", discoveredSources=[dict(source, failed=True)]))
        finally:
            processed.set()

    b, result_b = in_thread("stop-b", second)
    try:
        assert processed.wait(BOUND)
        assert capture.host.session.capture_record.seen["x"]["failed"] is False
    finally:
        drain_across_stop(tap, start, clock)
        a.join(BOUND)
        b.join(BOUND)
    assert not a.is_alive() and not b.is_alive() and "error" not in result_a and "error" not in result_b
    assert result_b["value"]["type"] == "error" and result_b["value"]["scope"] == "command"
    assert capture.host.session.manifest["state"] == ("completed" if already_seen else "interrupted")


@pytest.mark.parametrize("stall", ["capture-lock", "refusal-checkpoint"])
def test_resume_refusal_never_holds_broker_lock_while_waiting_on_a_writer(tmp_path, stall):
    taps = DrainingTaps()
    capture, _ = start_capture(tmp_path, taps, microphone=True, others_source="system")
    capture.call_generation = "generation"
    capture.mark_resume_ask("ask")
    service, identity, _ = broker(tmp_path, capture)
    service.policy = lambda: None
    browser = BrowserConnection(None)
    browser.resumes["ask"] = "stall"
    entered, release = threading.Event(), threading.Event()
    holder = None
    if stall == "capture-lock":
        holder, release = stall_holding(capture)
        refuse = capture.refuse_resume

        def refusing(*args, **kwargs):
            entered.set()
            return refuse(*args, **kwargs)

        capture.refuse_resume = refusing
    else:
        refuse = capture.host.session.refuse

        def refusing(*args):
            entered.set()
            assert release.wait(BOUND)
            return refuse(*args)

        capture.host.session.refuse = refusing
    reply, result = in_thread(
        "resume-result",
        lambda: service.update(browser, dict(type="result", requestId="ask", ok=False, code="deviceUnavailable")),
    )
    stopper = None
    try:
        assert entered.wait(BOUND)

        def stop():
            with service.lock:
                service._cancel_capture("stall", identity, "user")

        stopper, stopped = in_thread("broker-stop", stop)
        assert capture.stop_requested.wait(3), "Broker Stop waited for the stalled resume-refusal writer"
    finally:
        release.set()
        for tap in taps.taps.values():
            tap.drained.set()
        if holder is not None:
            holder.join(BOUND)
        reply.join(BOUND)
        if stopper is not None:
            stopper.join(BOUND)
        service.workers.shutdown(wait=True)
    assert not reply.is_alive() and "error" not in result, result
    assert stopper is not None and not stopper.is_alive() and "error" not in stopped, stopped


def test_native_stop_armed_side_respects_the_committed_selection_boundary(tmp_path):
    from test_native_sides import close
    from test_others_armed import QUIET, start

    recorder, _sides, _signals = start(tmp_path, QUIET)
    try:
        with recorder._lock:
            recorder.session.manifest["events"].append(
                dict(type="selection", atMs=20, me=True, others=True, reason="user")
            )
            attempt = recorder._pending_sources["system"]
            recorder.stopping, recorder._stop_at_ms = True, 15
            admitted = recorder._admit("system", attempt, 10)
            assert not admitted, "Buffer before Stop was clamped to a track starting after Stop"
            assert "system" not in recorder._track_ids
    finally:
        recorder.stopping = False  # Undo the manually staged Stop before real teardown.
        close(recorder)


def test_native_eos_keeps_its_full_drain_budget_after_a_checkpoint_stall(tmp_path, monkeypatch):
    from gi.repository import GLib

    recorder = native_recorder.NativeRecorder()
    recorder.session = RecordingSession(tmp_path)
    recorder.session.add_track("microphone", "Me", "microphone", 48000, 1)
    recorder.session.append_native("microphone", bytes(960), 0)
    recorder.pipeline = object()
    recorder._branches = {"microphone": object()}
    recorder._track_ids = {"microphone": "microphone"}
    now, timers, eos = [0], [], []
    idle = queue.Queue()
    recorder._elapsed_ms = lambda _pipeline: 0
    monkeypatch.setattr(
        GLib, "timeout_add_seconds", lambda seconds, callback: timers.append((now[0], seconds, callback)) or 0
    )
    monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: idle.put((callback, args)) or 0)
    monkeypatch.setattr(native_recorder, "request_eos", lambda *_: eos.append(now[0]))
    held, release = threading.Event(), threading.Event()

    def hold():
        with recorder._lock:
            held.set()
            assert release.wait(BOUND)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(BOUND)
        recorder.stop()
        now[0] = 9000  # Nine seconds of checkpoint stall, no wall-clock wait.
        release.set()
        holder.join(BOUND)
        callback, args = idle.get(timeout=BOUND)
        callback(*args)
        assert eos == [9000] and len(timers) == 1
        registered, seconds, _callback = timers[0]
        assert registered + seconds * 1000 - eos[0] == 10000, (
            "EOS inherited time already spent waiting for the writer lock"
        )
    finally:
        release.set()
        holder.join(BOUND)
        recorder._executor.shutdown(wait=True)
        recorder.pipeline = None
        recorder.session.finish()


def bound_broker(tmp_path, capture):
    service, identity, finished = broker(tmp_path, capture)
    browser = BrowserConnection(None, browser_instance=identity[0])
    browser.hello_seen = browser.status_seen = True
    browser.capabilities.add("independent-sessions-v1")
    browser.meetings[identity[1]] = dict(tabId=identity[1], title="Bound call", url=identity[2], sourceCount=1)
    browser.states[identity[1]] = dict(tabId=identity[1], recording=True, captureId=capture.capture_id)
    service.browsers[browser.id] = browser
    capture.call_identity = identity
    identifier = service._meeting_id(browser, identity[1])

    def reply(packet):
        with service.lock:
            service._command_result(
                browser,
                dict(
                    type="result",
                    requestId=packet["requestId"],
                    ok=True,
                    status=dict(recording=False, tabId=identity[1]),
                ),
            )

    browser.send = reply
    return service, browser, identity, identifier, finished


@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("target", ["call", "capture", "expected-capture"])
def test_bound_broker_stop_keeps_explicit_user_intent(tmp_path, user, target):
    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    tap, _ = record_live(capture, clock)
    service, browser, identity, identifier, finished = bound_broker(tmp_path, capture)
    tap.drained.set()
    try:
        assert browser.browser_instance == identity[0]
        assert service._bound_capture(identity) is capture
        selected = "capture:" + capture.capture_id if target == "capture" else identifier
        options = dict(expected_capture=capture.capture_id) if target == "expected-capture" else {}
        service.command(selected, "stop", user=user, **options)
        assert finished.wait(BOUND)
        assert capture.user_stopped is user
        assert service._stopped_identity(identity) is user
    finally:
        service.workers.shutdown(wait=True)
        capture.stop()


@pytest.mark.parametrize("superseded", ["newer-command", "call-version"])
def test_superseded_bound_start_cancel_does_not_invent_a_user_stop(tmp_path, superseded):
    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    tap, _ = record_live(capture, clock)
    service, browser, identity, logical, finished = bound_broker(tmp_path, capture)
    tap.drained.set()
    browser.commands["start"] = dict(
        action="start", tabId=identity[1], serial=1, expected=identity, logical=logical, version=0
    )
    if superseded == "newer-command":
        browser.commands["newer"] = dict(action="stop", tabId=identity[1], serial=2)
    else:
        service._call_versions[logical] = 1
    try:
        assert service._bound_capture(identity) is capture
        assert not service._stopped_identity(identity)
        with service.lock:
            service._command_result(
                browser,
                dict(
                    type="result",
                    requestId="start",
                    ok=True,
                    status=dict(recording=True, tabId=identity[1], captureId=capture.capture_id),
                ),
            )
        assert finished.wait(BOUND)
        assert capture.user_stopped is False
        assert service._stopped_identity(identity) is False
    finally:
        service.workers.shutdown(wait=True)
        capture.stop()


@pytest.mark.parametrize("target", ["call", "capture"])
def test_auto_opt_in_does_not_resurrect_a_previous_bound_user_stop(tmp_path, monkeypatch, target):
    """Opt-in while a stopped managed capture still publishes starts the call at once.

    The capture records into a managed Library session, so Stop returns while
    publication runs (stopping, not finished). The publication is gated on an
    event: the companion's stopped status and the opt-in arrive in that window,
    and the old save's completion only afterwards.
    """
    from concurrent.futures import Future

    from cassini_talk import library_publication
    from cassini_talk.library_publication import capture_parent, publish_stopped

    published = threading.Event()

    def gated_submit(session, *, cancel=None):
        future = Future()

        def run():
            published.wait(BOUND)
            try:
                future.set_result(publish_stopped(session, cancel=cancel))
            except Exception as error:  # noqa: BLE001 - delivered through the future
                future.set_exception(error)

        threading.Thread(target=run, daemon=True).start()
        return future

    monkeypatch.setattr(library_publication, "submit_stopped", gated_submit)
    taps = DrainingTaps()
    capture, clock = start_capture(
        capture_parent(tmp_path / "Library"), taps, microphone=True, others_source="browser"
    )
    tap, _ = record_live(capture, clock)
    service, browser, identity, logical, finished = bound_broker(tmp_path, capture)
    # The call has a generation (D52.5), so the generation guard is exercised too.
    browser.meetings[identity[1]]["callGeneration"] = capture.call_generation = 7
    tap.drained.set()
    automatic, sent = [], []
    submit, send = service.workers.submit, browser.send

    def hold_automatic(function, *args, **kwargs):
        if function == service._automatic:
            automatic.append((function, args, kwargs))
            return Future()
        return submit(function, *args, **kwargs)

    def observe_send(packet):
        sent.append(packet)
        return send(packet)

    monkeypatch.setattr(service.workers, "submit", hold_automatic)
    monkeypatch.setattr(browser, "send", observe_send)
    try:
        service.command("capture:" + capture.capture_id if target == "capture" else logical, "stop", user=True)
        assert capture.stopping and not capture.finished and not finished.is_set(), "Stop must leave it publishing"
        assert service._stopped_identity(identity)
        assert capture.host.session.manifest["events"][-1]["reason"] == "user"
        if target == "capture":
            # The companion reports its stopped session independently of the
            # native owner's publication and completion callback.
            service.update(browser, dict(type="status", recording=False, tabId=identity[1]))
        # A publishing capture is not recording its call: no synthesized state.
        assert not browser.states.get(identity[1], {}).get("recording"), browser.states
        service.policy()
        assert automatic == [], "Without opt-in the user's Stop still suppresses a restart while saving"
        service.configure(True)  # Explicit opt-in resets the previous Stop.
        assert not service._stopped_identity(identity)
        assert capture.stopping and not capture.finished
        assert len(automatic) == 1, (
            browser.states,
            browser.meetings,
            browser.attempted,
            service._scheduled_calls,
            service._stopping_all.is_set(),
        )
        published.set()  # The old save's completion arrives afterwards.
        assert finished.wait(BOUND)
        assert capture.publication is not None and capture.error is None
        function, args, kwargs = automatic.pop(0)
        function(*args, **kwargs)
        actual = (service._stopped_identity(identity), [p["action"] for p in sent])
        assert actual == (False, ["stop", "start"] if target == "call" else ["start"]), actual
    finally:
        published.set()
        service.workers.shutdown(wait=True)
        capture.stop()


@pytest.mark.parametrize("route", ["browser", "broker"])
def test_auto_opt_in_preserves_a_later_bound_user_stop(tmp_path, route):
    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    tap, _ = record_live(capture, clock)
    service, _browser, identity, logical, finished = bound_broker(tmp_path, capture)
    tap.drained.set()
    try:
        service.configure(True)  # This capture has not stopped yet.
        if route == "broker":
            service.command(logical, "stop", user=True)
        else:
            capture.handle(dict(type="stop", captureId=capture.capture_id, reason="user"))
        assert finished.wait(BOUND)
        assert capture.user_stopped is True and service._stopped_identity(identity) is True
    finally:
        service.workers.shutdown(wait=True)
        capture.stop()
