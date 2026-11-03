# SPDX-License-Identifier: GPL-3.0-or-later
"""Run shared literal D33 actions through the real broker and session writer."""

import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest
from test_browser_capture import Tap

from cassini_talk.browser_capture import BrowserCapture
from cassini_talk.browser_service import PCM_STALE_AFTER, AudioTransport, BrowserConnection, BrowserService
from cassini_talk.call_lifecycle import BROWSER_RETURN_GRACE, CALL_END_GRACE
from cassini_talk.session_events import source_state

VECTORS = Path(__file__).resolve().parents[1] / "browser-protocol/conformance/control/call-lifecycle-vectors.json"
PCM_FRESH = 2  # browser_capture.browser_ready_since's window (SPEC.md)
HOLD_BOUND = 10  # seconds; only a bound on waiting for a held fallback's thread


class HeldTap(Tap):
    """A fallback device that has not delivered its first buffer yet.

    NativeTap.start returns once the device admits a first buffer, so the
    fallback is decided but not committed while this start waits.
    """

    def __init__(self, opening, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opening, self.first_buffer = opening, threading.Event()

    def start(self):
        self.opening.set()
        if not self.first_buffer.wait(HOLD_BOUND):
            raise OSError("The held fallback device was never released")
        super().start()


class Inline:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
        return future


@pytest.mark.parametrize("case", json.loads(VECTORS.read_text())["cases"], ids=lambda case: case["name"])
def test_shared_actions_use_actual_capture_devices_spans_and_end(tmp_path, monkeypatch, case):
    assert json.loads(VECTORS.read_text())["constants"] == dict(
        CALL_END_GRACE=CALL_END_GRACE,
        BROWSER_RETURN_GRACE=BROWSER_RETURN_GRACE,
    )
    now = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    service = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    service.workers.shutdown()
    service.workers = Inline()
    # fallbackCommitted:false: the next system tap holds its first buffer.
    held = dict(next=False, tap=None, closer=None, opening=threading.Event())

    def taps(kind, *args, **kwargs):
        if kind == "system" and held["next"]:
            held["next"] = False
            held["tap"] = HeldTap(held["opening"], kind, *args, **kwargs)
            return held["tap"]
        return Tap(kind, *args, **kwargs)

    def lost_written():
        current = source_state(capture.host.session.manifest.get("events"))
        return (current.source, current.reason) == ("system", "lost")

    capture = BrowserCapture(service._capture_changed, parent=tmp_path / "Library", tap_factory=taps)
    capture.start(
        dict(
            type="start",
            captureId="call",
            timelineOriginMs=int(time.time() * 1000),
            microphone=True,
            callGeneration=case.get("callGeneration"),
        ),
        others_source=case.get("source", "browser"),
        reason=case.get("sourceReason", "start"),
    )
    capture.elapsed_ms = lambda: now[0] * 1000
    if case.get("source", "browser") == "browser" and not case.get("multistream"):
        capture.host.handle(dict(type="track", id="original", kind="remote", sampleRate=48000, channels=1))
        capture.host.handle(dict(type="audio", id="original", sequence=0, offsetMs=0, pcm="AQACAA=="))
    if not case.get("others", True):
        capture.select(True, False)
    service.captures["call"] = capture
    browser = BrowserConnection(None)
    browser.capabilities = {"independent-sessions-v1", "page-state-v1"}
    if case.get("multistream"):
        browser.capabilities.add("multi-stream-v1")
    if case.get("callEvidence"):
        browser.capabilities.add("call-evidence-v1")
    if case.get("resumeCapture"):
        browser.capabilities.add("resume-capture-v1")
    browser.status_seen = True
    browser.states = {3: dict(recording=True, tabId=3, captureId="call")}
    service.browsers[browser.id] = browser
    connection = 0
    commands, actions, sent = [], [], []
    monkeypatch.setattr(service, "command", lambda *args, **kwargs: commands.append((args, kwargs)))
    # D52.5 asks go straight to the control peer, never through command().
    monkeypatch.setattr(BrowserConnection, "send", lambda _peer, packet: sent.append(packet))
    lifecycle = service._capture_lifecycle

    def record(*args):
        actions.append(args[-1])
        return lifecycle(*args)

    monkeypatch.setattr(service, "_capture_lifecycle", record)
    folder, microphone = capture.directory, capture.microphone
    # The recording's audio transport, as _audio registers it. The runner only
    # emulates the peer's socket; deadlines and teardown are the broker's own.
    transport = AudioTransport(None, service.clock)
    transport.capture = capture
    service._audio_transports.add(transport)

    # The runner's own account of the current transport, never read back
    # from the broker: whether a span got PCM on it, and its last frame.
    fed = dict(pcm=False, last=None)

    def deliver(packet):
        capture.handle(packet)
        transport.last_inbound = fed["last"] = now[0]
        if packet["type"] == "track":
            transport.spans.add(packet["id"])
        else:
            fed["pcm"] = True  # The runner never ends a span: this one stays open.

    first = case["steps"][0]
    if (
        case.get("multistream")
        and case.get("source", "browser") == "browser"
        and first.get("heard")
        and first.get("sourceCount", 0) > 0
    ):
        # A heard call's receiver has delivered PCM on the transport: its open
        # span arms the PCM_STALE_AFTER watchdog from this frame on. Without
        # pcmLive the first row is not a live sample, so the frame is the
        # latest one outside the 2 s PCM_FRESH window.
        now[0] = first["atMs"] / 1000 - PCM_FRESH - 0.001
        deliver(dict(type="track", id="original", kind="remote", sampleRate=48000, channels=1))
        deliver(dict(type="audio", id="original", sequence=0, offsetMs=first["atMs"], pcm="AQACAA=="))
    # Once its audio transport closed, only a native resume (D52.3c) attaches
    # a new one: a new start with its captureId is refused.
    transport_closed = False
    try:
        for index, step in enumerate(case["steps"]):
            now[0] = step["atMs"] / 1000
            committed = step.get("fallbackCommitted", True)
            if held["closer"] is not None and committed:
                # The fallback device delivers its first buffer now: the
                # broker's own fallback commits and writes system/lost.
                held["tap"].first_buffer.set()
                held["closer"].join(HOLD_BOUND)
                assert not held["closer"].is_alive(), (case["name"], step)
                held["closer"] = None
                assert capture.browser_lost and lost_written(), (case["name"], step, capture.error)
                # Written at the commit, not when the transport closed.
                event = next(e for e in capture.host.session.manifest["events"] if e.get("reason") == "lost")
                assert event["atMs"] >= step["atMs"], (case["name"], step, event)
            if step.get("controlConnection", connection) != connection:
                # A new hello is a new control peer; the recording is matched
                # by its call, never by the connection it started under.
                connection = step["controlConnection"]
                service.browsers.pop(browser.id, None)
                replacement = BrowserConnection(None)
                replacement.capabilities, replacement.status_seen = set(browser.capabilities), True
                replacement.states = {3: dict(recording=True, tabId=3, captureId="call")}
                browser = replacement
            same = step.get("sameTab", True)
            page = step["pageState"] if same else None
            count = step.get("sourceCount", 0) if same else 0
            browser.states[3]["pageState"] = page
            browser.meetings = {
                3: dict(pageState=page, sourceCount=count, heard=step.get("heard", not case.get("multistream", False)))
            }
            browser.meetings[3]["sourceCountKnown"] = step.get("sourceCountKnown", True)
            if "callGeneration" in step:
                browser.meetings[3]["callGeneration"] = step["callGeneration"]
            if not step.get("rowPresent", True):
                browser.meetings = {}
            if step.get("controlAlive", True):
                service.browsers[browser.id] = browser
            else:
                service.browsers.pop(browser.id, None)
            actions.clear()
            asked = len(sent)
            if not step.get("transportAlive", True) and not transport_closed:
                # D52.4: the transport closed by EOF, error or the watchdog.
                # When the runner fed an open span PCM on it and then nothing
                # for PCM_STALE_AFTER, the vector means the app's own watchdog
                # (SPEC.md), on a fallback too (D57): the broker must find it
                # armed and stale by itself. Only otherwise does the runner
                # emulate an EOF. Either way the broker's real teardown runs.
                transport_closed = True
                lost = capture.browser_lost
                if case.get("multistream") and fed["pcm"] and now[0] - fed["last"] >= PCM_STALE_AFTER:
                    assert capture.remote_open(), (case["name"], step, "an open span with PCM is armed")
                    assert transport.expired(now[0]) == "stopped sending audio", (case["name"], step)
                if committed:
                    service._audio_closed(transport, capture)
                else:
                    # The broker's real teardown decides the fallback and
                    # blocks opening its device, as on the reader thread.
                    held["next"] = True
                    held["closer"] = threading.Thread(target=service._audio_closed, args=(transport, capture))
                    held["closer"].start()
                    assert held["opening"].wait(HOLD_BOUND), (case["name"], step, capture.error)
                    actions.append("fallback")
                if capture.browser_lost and not lost:
                    actions.append("fallback")
            if not committed:
                # Decided, not committed: the recorded source is unchanged.
                assert held["closer"] is not None and held["closer"].is_alive(), (case["name"], step)
                assert not capture.browser_lost and not lost_written(), (case["name"], step)
            if step.get("pcmLive") and transport_closed and not capture.finished:
                # pcmLive implies a live transport: after a close only the real
                # native resume attaches one, to the same folder and clock. Do
                # not require an ask first: cases without resumeCapture model
                # a transport that came back by other means.
                transport = AudioTransport(None, service.clock)
                fed.update(pcm=False, last=None)
                service._audio_transports.add(transport)
                target, proof = service._resume_attach(
                    dict(
                        type="resume",
                        version=1,
                        independent=True,
                        captureId="call",
                        timelineOriginMs=capture.origin_ms,
                        callGeneration=capture.call_generation,
                        features=["source-outcomes-v1", "resume-capture-v1"],
                    ),
                    transport,
                )
                assert target is capture and transport.capture is capture
                assert proof == dict(
                    type="started",
                    path=str(folder),
                    features=["resume-capture-v1"],
                    resumed=True,
                    captureId="call",
                    timelineOriginMs=capture.origin_ms,
                    callGeneration=capture.call_generation,
                )
                transport_closed = False
            if step.get("pcmLive") and not transport_closed:
                # pcmLive is a point sample of the 2 s PCM_FRESH window. Take
                # the latest packet time a not-live row 1 ms later still allows.
                now[0] = step["atMs"] / 1000 - PCM_FRESH + 0.0005
                wire = "live-" + str(index)
                deliver(dict(type="track", id=wire, kind="remote", sampleRate=48000, channels=1))
                deliver(dict(type="audio", id=wire, sequence=0, offsetMs=step["atMs"], pcm="AQACAA=="))
                now[0] = step["atMs"] / 1000
            # Only real PCM on the retained live transport makes restore ready.
            if (
                not capture.finished
                and count
                and capture.browser_lost
                and not transport_closed
                and step.get("restoreReady", True)
            ):
                wire = "return-" + str(index)
                deliver(dict(type="track", id=wire, kind="remote", sampleRate=48000, channels=1))
                deliver(dict(type="audio", id=wire, sequence=0, offsetMs=step["atMs"], pcm="AQACAA=="))
            if step.get("resumeRefused"):
                # The extension's answer to this connection's pending ask.
                (pending,) = [packet for packet in sent if "resumeCapture" in packet][-1:]
                service.update(
                    browser,
                    dict(type="result", requestId=pending["requestId"], ok=False, error="Target is no longer active."),
                )
            service.policy()
            assert actions == step["expect"], (case["name"], step, capture.error)
            # Exactly one resume Start per askResume, and none otherwise: a
            # pending or refused ask is never repeated on a later tick.
            new = sent[asked:]
            assert len(new) == step["expect"].count("askResume"), (case["name"], step, new)
            for packet in new:
                assert {key: packet[key] for key in packet if key != "requestId"} == dict(
                    type="command",
                    action="start",
                    mode="tracks",
                    tabId=3,
                    independent=True,
                    captureLocal=False,
                    callGeneration=case["callGeneration"],
                    keepOnNavigation=True,
                    resumeCapture=dict(captureId="call", timelineOriginMs=capture.origin_ms),
                )
            assert capture.directory == folder and capture.microphone is microphone
            if not capture.finished:
                assert not any(e["type"] == "end" for e in capture.host.session.manifest["events"])
        ends = [e for e in capture.host.session.manifest["events"] if e["type"] == "end"]
        stops = [s for s in case["steps"] if s["expect"] and s["expect"][0].startswith("stop:")]
        assert len(ends) == len(stops)
        if stops:
            assert ends[0]["reason"] == stops[0]["expect"][0].split(":")[1]
        if case["name"] == "closed-tab-never-starts-computer-sound":
            assert not any(t["kind"] == "system" for t in capture.host.session.manifest["tracks"])
        if any(s["expect"] == ["restore"] for s in case["steps"]):
            restored = [e for e in capture.host.session.manifest["events"] if e.get("reason") == "restored"]
            assert len(restored) == 1
            assert capture.host.session.manifest["tracks"][0]["kind"] == "microphone"
        assert len(list((tmp_path / "Library").iterdir())) == 1
        assert all(args[1] == "stop" for args, _kwargs in commands)
    finally:
        if held["tap"] is not None:
            held["tap"].first_buffer.set()
        if held["closer"] is not None:
            held["closer"].join(HOLD_BOUND)
        capture.stop()


@pytest.mark.parametrize("known", ["row", "unknown-count", "missing-row"])
def test_multistream_only_known_zero_counts_start_the_thirty_second_timer(tmp_path, monkeypatch, known):
    unknown = dict(sourceCountKnown=False) if known == "unknown-count" else dict(rowPresent=False)
    steps = [dict(atMs=0, sourceCount=1, heard=True)]
    if known != "row":
        steps.extend(
            [
                dict(atMs=1000, sourceCount=0),
                dict(atMs=2000, sourceCount=0, **unknown),
                dict(atMs=50000, sourceCount=0, **unknown),
            ]
        )
        start = 51000
    else:
        start = 1000
    steps.extend(
        [
            dict(atMs=start, sourceCount=0),
            dict(atMs=start + 29999, sourceCount=0),
            dict(atMs=start + 30000, sourceCount=0, expect=["stop:callEnded"]),
        ]
    )
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name=known,
            multistream=True,
            steps=[
                dict(pageState="present", expect=[], **step)
                if "expect" not in step
                else dict(pageState="present", **step)
                for step in steps
            ],
        ),
    )


@pytest.mark.parametrize("heard", [False, True])
def test_multistream_rejoin_and_never_heard_calls_survive(tmp_path, monkeypatch, heard):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="rejoin" if heard else "never-heard",
            multistream=True,
            steps=[
                dict(atMs=0, pageState="present", sourceCount=1, heard=heard, expect=[]),
                dict(atMs=1000, pageState="present", sourceCount=0, heard=False, expect=[]),
                dict(atMs=15999, pageState="present", sourceCount=0, heard=False, expect=[]),
                dict(atMs=16000, pageState="present", sourceCount=1, heard=heard, expect=[]),
                dict(atMs=60000, pageState="present", sourceCount=1 if heard else 0, heard=False, expect=[]),
            ],
        ),
    )


def test_multistream_control_loss_with_remote_pcm_does_not_fall_back():
    from cassini_talk.call_lifecycle import CallLifecycle

    lifecycle = CallLifecycle()
    for now in (0, 10, 150):
        assert (
            lifecycle.decide(
                now,
                None,
                0,
                multistream=True,
                heard=now == 0,
                inventory_known=False,
                control_alive=False,
                pcm_live=True,
                fallback=False,
                others=True,
                restore_ready=False,
            )
            is None
        )
        assert lifecycle.missing is lifecycle.ended is None


def test_multistream_actual_browser_pcm_keeps_control_loss_from_recording_system_audio(tmp_path, monkeypatch):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="control-only-loss-with-real-pcm",
            multistream=True,
            steps=[
                dict(atMs=0, pageState="present", sourceCount=1, heard=True, expect=[]),
                dict(atMs=1000, pageState="unknown", sourceCount=0, controlAlive=False, pcmLive=True, expect=[]),
                dict(atMs=150000, pageState="unknown", sourceCount=0, controlAlive=False, pcmLive=True, expect=[]),
            ],
        ),
    )


def test_multistream_fallback_expires_despite_discarded_browser_packets(tmp_path, monkeypatch):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="discarded-wire-pcm-cannot-extend-fallback",
            multistream=True,
            steps=[
                dict(atMs=0, pageState="present", sourceCount=1, heard=True, expect=[]),
                dict(atMs=3000, pageState="unknown", sourceCount=0, controlAlive=False, expect=[]),
                dict(atMs=4999, pageState="unknown", sourceCount=0, controlAlive=False, expect=[]),
                dict(atMs=5000, pageState="unknown", sourceCount=0, controlAlive=False, expect=["fallback"]),
                dict(atMs=122999, pageState="unknown", sourceCount=0, controlAlive=False, pcmLive=True, expect=[]),
                dict(
                    atMs=123000,
                    pageState="unknown",
                    sourceCount=0,
                    controlAlive=False,
                    pcmLive=True,
                    expect=["stop:pageGone"],
                ),
            ],
        ),
    )


def test_returning_prejoin_receive_slots_do_not_reset_the_deadline(tmp_path, monkeypatch):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="return-prejoin",
            callEvidence=True,
            steps=[
                dict(atMs=0, pageState="navigating", sourceCount=0, expect=[]),
                dict(atMs=2000, pageState="navigating", sourceCount=0, expect=["fallback"]),
                dict(atMs=119999, pageState="present", sourceCount=3, heard=False, restoreReady=False, expect=[]),
                dict(
                    atMs=120000,
                    pageState="present",
                    sourceCount=3,
                    heard=False,
                    restoreReady=False,
                    expect=["stop:pageGone"],
                ),
            ],
        ),
    )


def test_hangup_after_failed_restore_keeps_the_normal_call_end_grace(tmp_path, monkeypatch):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="hangup-after-restore-failure",
            steps=[
                dict(atMs=0, pageState="navigating", sourceCount=0, expect=[]),
                dict(atMs=2000, pageState="navigating", sourceCount=0, expect=["fallback"]),
                dict(atMs=60000, pageState="present", sourceCount=2, restoreReady=False, expect=[]),
                dict(atMs=61000, pageState="present", sourceCount=0, expect=[]),
                dict(atMs=63000, pageState="present", sourceCount=0, expect=["stop:callEnded"]),
            ],
        ),
    )


def test_multistream_reload_through_present_zero_keeps_the_two_minute_return_deadline(tmp_path, monkeypatch):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="reload-present-zero",
            multistream=True,
            steps=[
                dict(atMs=0, pageState="present", sourceCount=1, heard=True, expect=[]),
                dict(atMs=1000, pageState="navigating", sourceCount=0, expect=[]),
                dict(atMs=2000, pageState="present", sourceCount=0, expect=[]),
                dict(atMs=3000, pageState="present", sourceCount=0, expect=["fallback"]),
                dict(atMs=32000, pageState="present", sourceCount=0, expect=[]),
                dict(atMs=120999, pageState="present", sourceCount=0, expect=[]),
                dict(atMs=121000, pageState="present", sourceCount=0, expect=["stop:pageGone"]),
            ],
        ),
    )


D52_VECTORS = VECTORS.parents[1] / "multistream/lifecycle.json"


D52_CASES = json.loads(D52_VECTORS.read_text())["cases"]


def d52(case):
    """A resume attaches only to the recording's own callGeneration (R3).

    Cases without resumeCapture have none, and two of them restore through a
    new transport; give such a case and its steps one default. No case sets a
    step's generation without the case's, and the oracle latches heard for any
    generation when the case has none, so this changes no expectation.
    """
    generation = case.get("callGeneration")
    steps = [dict(step, pageState=step.get("pageState", "present")) for step in case["steps"]]
    if generation is None:
        generation = "call-default"
        steps = [dict(step, callGeneration="call-default") for step in steps]
    return dict(case, multistream=True, callGeneration=generation, steps=steps)


@pytest.mark.parametrize("case", D52_CASES, ids=lambda case: case["name"])
def test_d52_shared_lifecycle_on_actual_capture(tmp_path, monkeypatch, case):
    test_shared_actions_use_actual_capture_devices_spans_and_end(tmp_path, monkeypatch, d52(case))
