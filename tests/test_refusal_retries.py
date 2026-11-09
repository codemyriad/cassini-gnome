# SPDX-License-Identifier: GPL-3.0-or-later
"""R82 (G13c): the D58 #2 stand-in adopted by D49's ask-once, refused resumes,
Try again on refused native sides of a browser capture, the live-line coverage
rule, and the D17 per-meeting lines."""

import json
import time
import uuid

import pytest
from test_browser_capture import Tap
from test_browser_resume_policy import asks, close, peer, policy, row, run, scheduled_asks  # noqa: F401
from test_refusal_vectors import broker as served  # noqa: F401
from test_resume_capture_host import VECTORS, payload_identity  # noqa: F401
from test_resume_capture_host import broker as wire  # noqa: F401
from test_voice_application import application_class  # noqa: F401

from cassini_talk import refusals
from cassini_talk.browser_capture import ResumeRefused
from cassini_talk.browser_service import AudioTransport
from cassini_talk.session_events import source_state

NO_SOURCES = dict(ok=False, error="No audio is available from that meeting yet.", code="noSources")


def stand_in(service, parent, browser, **meeting):
    """The real D58 #2 path: a rejected Start falls back into a system/missing stand-in."""
    service.tap_factory, service.capture_parent = Tap, parent
    service.update(browser, dict(type="meetings", meetings=[row(**meeting)]))
    service.update(browser, dict(type="status", recording=False, sessions=[]))
    (shown,) = service.snapshot()["meetings"]
    snapshot = service.start_fallback(shown["id"], row(**meeting), True, refusals=[refusals.from_result(NO_SOURCES)])
    capture = service.captures[snapshot["captureId"]]
    assert capture.browser_lost
    return capture


def ticks(service, tasks, browser, count=3, **meeting):
    for _ in range(count):
        service.update(browser, dict(type="meetings", meetings=[row(**meeting)]))
        service.update(browser, dict(type="status", recording=False, sessions=[]))
        service.policy()
        run(tasks)


def folders(parent):
    return sorted(path for path in parent.rglob("Recording-*") if path.is_dir())


def sources(capture):
    return [(e["source"], e["reason"]) for e in capture.host.session.manifest["events"] if e["type"] == "othersSource"]


# 1a. The stand-in is adopted by D49's ask-once.


def test_the_stand_in_asks_once_on_a_connection_once_the_call_is_heard(policy):  # noqa: F811
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = stand_in(service, parent, browser, heard=False)
    try:
        assert sources(capture) == [("system", "missing")]
        ticks(service, tasks, browser, heard=False)
        assert not asks(sent), "never before the call is heard"
        ticks(service, tasks, browser)
        (packet,) = asks(sent)
        assert packet == dict(
            type="command",
            requestId=packet["requestId"],
            action="start",
            mode="tracks",
            tabId=3,
            independent=True,
            captureLocal=False,
            callGeneration="call-A",
            keepOnNavigation=True,
            resumeCapture=dict(
                captureId=capture.capture_id,
                timelineOriginMs=capture.origin_ms,
                lostAtMs=source_state(capture.host.session.manifest["events"]).since_ms,
            ),
        )
        ticks(service, tasks, browser, count=5)
        assert len(asks(sent)) == 1, "once per stand-in and connection"
        lifecycle = service._capture_lifecycles[capture.capture_id]
        assert lifecycle.asked_loss == 0 and capture.loss_serial == 0, "no D52.5 ask is spent"
        # A reconnect asks once more, under the new owner peer.
        close(service, browser)
        returning = peer(service)
        ticks(service, tasks, returning)
        assert len(asks(sent)) == 2 and sent[-1][0] is returning
        assert folders(parent) == [capture.directory]
    finally:
        capture.stop()


@pytest.mark.parametrize(
    "change",
    [
        dict(sourceCount=0),
        dict(pageState="navigating"),
        dict(callGeneration="call-B"),
    ],
    ids=["no-sources", "not-present", "another-call"],
)
def test_the_stand_in_does_not_ask_when_a_condition_fails(policy, change):  # noqa: F811
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = stand_in(service, parent, browser)
    try:
        sent.clear()
        ticks(service, tasks, browser, **change)
        assert not asks(sent), change
    finally:
        capture.stop()


def test_the_stand_in_asks_without_a_system_tap(policy):  # noqa: F811
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = stand_in(service, parent, browser)
    try:
        # Computer sound refused too: nothing records the others; a resume matters most.
        capture.native_taps.pop("system", None)
        ticks(service, tasks, browser)
        assert len(asks(sent)) == 1
    finally:
        capture.stop()


def attach(service, capture):
    transport = AudioTransport(None, service.clock)
    service._resume_attach(
        dict(
            type="resume",
            version=1,
            independent=True,
            captureId=capture.capture_id,
            timelineOriginMs=capture.origin_ms,
            callGeneration="call-A",
            features=["resume-capture-v1"],
        ),
        transport,
    )
    return transport


def test_an_adopted_stand_in_switches_at_the_seam_and_a_later_loss_asks_again(policy):  # noqa: F811
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = stand_in(service, parent, browser)
    try:
        ticks(service, tasks, browser)
        assert len(asks(sent)) == 1
        attach(service, capture)
        assert capture.resume_asks == {}, "an attach answers the ask"
        capture.handle(dict(type="track", id="back", kind="remote", label="Guest", sampleRate=48000, channels=1))
        capture.handle(dict(type="audio", id="back", sequence=0, offsetMs=capture.elapsed_ms(), pcm="AQACAA=="))
        ticks(service, tasks, browser, count=1)
        assert sources(capture)[-1] == ("browser", "callConnected")
        assert not capture.browser_lost and folders(parent) == [capture.directory]
        assert "refusals" in capture.host.session.manifest  # Only the start's browser refusal.
        assert [r["detail"] for r in capture.host.session.manifest["refusals"]] == ["noSources"]
        # A later loss is D52.5's: its own ask (loss 1).
        capture.close()
        assert capture.browser_lost and capture.loss_serial == 1
        ticks(service, tasks, browser)
        assert len(asks(sent)) == 2
    finally:
        capture.stop()


# 1b. A refused resume is recorded once per ask.


def answer(service, browser, packet, **result):
    service.update(browser, dict(type="result", requestId=packet["requestId"], **result))


def resume_refusals(capture):
    return [r for r in capture.host.session.manifest.get("refusals", []) if r.get("detail") == "resumeRefused"]


def test_an_ok_false_resume_is_one_refusal_with_its_code_at_the_ask(policy):  # noqa: F811
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = stand_in(service, parent, browser)
    try:
        ticks(service, tasks, browser)
        (packet,) = asks(sent)
        at = capture.resume_asks[packet["requestId"]]
        answer(service, browser, packet, ok=False, error="Target is no longer active.", code="gone")
        assert resume_refusals(capture) == [
            dict(atMs=at, side="browser", code="startRejected", detail="resumeRefused", message="gone")
        ]
        # The same reply again, or a late native refusal, writes nothing more.
        answer(service, browser, packet, ok=False, code="gone")
        refuse_native(service, capture, requestId=packet["requestId"])
        assert len(resume_refusals(capture)) == 1
        saved = json.loads((capture.directory / "session.json").read_text())
        assert [r.get("detail") for r in saved["refusals"]] == ["noSources", "resumeRefused"]
        assert sources(capture) == [("system", "missing")], "a refusal changes nothing else"
    finally:
        capture.stop()


def refuse_native(service, capture, **request):
    """A native resume on the audio pipe that the target refuses (a wrong origin)."""
    packet = dict(
        type="resume",
        version=1,
        independent=True,
        captureId=capture.capture_id,
        timelineOriginMs=capture.origin_ms + 1,
        callGeneration="call-A",
        features=["resume-capture-v1"],
        **request,
    )
    with pytest.raises(ResumeRefused, match="Target is no longer active."):
        service._resume_attach(packet, AudioTransport(None, service.clock))


@pytest.mark.parametrize("order", ["native-first", "reply-first"])
def test_a_native_refusal_and_an_ok_false_reply_give_one_entry(policy, order):  # noqa: F811
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = stand_in(service, parent, browser)
    try:
        ticks(service, tasks, browser)
        (packet,) = asks(sent)
        if order == "native-first":
            refuse_native(service, capture, requestId=packet["requestId"])
            answer(service, browser, packet, ok=False, code="resumeRefused")
            message = "Target is no longer active."
        else:
            answer(service, browser, packet, ok=False, code="resumeRefused")
            refuse_native(service, capture, requestId=packet["requestId"])
            message = "resumeRefused"
        (item,) = resume_refusals(capture)
        assert item["message"] == message
    finally:
        capture.stop()


def test_two_open_connections_keep_one_ask_each(policy):  # noqa: F811
    service, tasks, sent, parent = policy
    first = peer(service)
    capture = stand_in(service, parent, first)
    try:
        ticks(service, tasks, first)
        (a,) = asks(sent)
        at_a = capture.resume_asks[a["requestId"]]
        # A second hello while the first connection is still open asks too.
        second = peer(service)
        ticks(service, tasks, second)
        assert len(asks(sent)) == 2 and sent[-1][0] is second
        b = asks(sent)[-1]
        at_b = capture.resume_asks[b["requestId"]]
        # The first ask's reply is still its own: exactly one entry, at its time.
        answer(service, first, a, ok=False, code="gone")
        assert resume_refusals(capture) == [
            dict(atMs=at_a, side="browser", code="startRejected", detail="resumeRefused", message="gone")
        ]
        assert list(capture.resume_asks) == [b["requestId"]]
        # The second ask's outcome is handled on its own.
        answer(service, second, b, ok=False, code="resumeRefused")
        assert resume_refusals(capture)[1:] == [
            dict(atMs=at_b, side="browser", code="startRejected", detail="resumeRefused", message="resumeRefused")
        ]
        assert capture.resume_asks == {}
    finally:
        capture.stop()


def overlapping(service, tasks, sent, parent):
    """Asks A and B pending at once, from two open control connections."""
    first = peer(service)
    capture = stand_in(service, parent, first)
    ticks(service, tasks, first)
    second = peer(service)
    ticks(service, tasks, second)
    a, b = asks(sent)
    assert sent[-1][0] is second
    return capture, dict(A=(first, a), B=(second, b))


NATIVE = "Target is no longer active."


@pytest.mark.parametrize(
    "steps",
    [
        "A-native,A-reply,B-native,B-reply",
        "B-native,A-native,B-reply,A-reply",
        "A-native,B-reply,A-reply",
        "A-reply,A-native,B-native,B-reply",  # The extension timed A out before its native resume.
        "B-reply,A-reply,B-native,A-native",
    ],
)
def test_overlapping_asks_get_one_entry_each_in_any_order(policy, steps):  # noqa: F811
    service, tasks, sent, parent = policy
    capture, pending = overlapping(service, tasks, sent, parent)
    try:
        at = {name: capture.resume_asks[packet["requestId"]] for name, (_, packet) in pending.items()}
        first = {}
        for step in steps.split(","):
            name, kind = step.split("-")
            browser, packet = pending[name]
            if kind == "native":
                refuse_native(service, capture, requestId=packet["requestId"])
            else:
                answer(service, browser, packet, ok=False, code="gone" + name)
            first.setdefault(name, NATIVE if kind == "native" else "gone" + name)
            # Each answer only ever settles its own ask.
            assert set(capture.resume_asks) == {pending[n][1]["requestId"] for n in pending if n not in first}
        assert resume_refusals(capture) == [
            dict(atMs=at[name], side="browser", code="startRejected", detail="resumeRefused", message=message)
            for name, message in first.items()
        ]
    finally:
        capture.stop()


def test_a_native_refusal_never_answers_another_pending_ask(policy):  # noqa: F811
    """Codex #174: A's native refusal while A and B are pending is A's alone."""
    service, tasks, sent, parent = policy
    capture, pending = overlapping(service, tasks, sent, parent)
    try:
        (first, a), (second, b) = pending["A"], pending["B"]
        at_a, at_b = capture.resume_asks[a["requestId"]], capture.resume_asks[b["requestId"]]
        refuse_native(service, capture, requestId=a["requestId"])
        assert [(r["atMs"], r["message"]) for r in resume_refusals(capture)] == [(at_a, NATIVE)]
        assert list(capture.resume_asks) == [b["requestId"]], "B is still pending"
        answer(service, first, a, ok=False, code="gone")  # Already answered: nothing more.
        assert len(resume_refusals(capture)) == 1
        answer(service, second, b, ok=False, code="resumeRefused")
        assert [(r["atMs"], r["message"]) for r in resume_refusals(capture)] == [
            (at_a, NATIVE),
            (at_b, "resumeRefused"),
        ]
        assert capture.resume_asks == {}
    finally:
        capture.stop()


@pytest.mark.parametrize(
    "identity", [{}, dict(requestId="unknown"), dict(requestId=7)], ids=["absent", "unknown", "integer"]
)
def test_a_native_refusal_without_a_known_ask_writes_nothing(policy, identity):  # noqa: F811
    service, tasks, sent, parent = policy
    capture, pending = overlapping(service, tasks, sent, parent)
    try:
        before = dict(capture.resume_asks)
        refuse_native(service, capture, **identity)
        assert resume_refusals(capture) == []
        assert capture.resume_asks == before and len(before) == 2, "both asks stay pending"
        # Each ask's own reply still records it.
        for browser, packet in pending.values():
            answer(service, browser, packet, ok=False, code="gone")
        assert [r["atMs"] for r in resume_refusals(capture)] == list(before.values())
    finally:
        capture.stop()


def ask_on(service, tasks, sent, capture, browser):
    """One more resume ask on this connection; returns its packet."""
    # GNOME asks once per connection and loss: a vector's second ask on one
    # connection stands for a policy that may ask that connection again.
    lifecycle = service._capture_lifecycles.get(capture.capture_id)
    if lifecycle is not None:
        lifecycle.asked_on.discard(browser.id)
    before = len(asks(sent))
    for _ in range(5):
        ticks(service, tasks, browser, count=1)
        if len(asks(sent)) > before:
            break
    assert len(asks(sent)) == before + 1 and sent[-1][0] is browser
    return asks(sent)[-1]


@pytest.mark.parametrize("case", VECTORS["askAttribution"]["cases"], ids=lambda case: case["name"])
def test_the_shared_ask_attribution_vectors_hold_with_a_fresh_id_per_ask(policy, case):  # noqa: F811
    """Codex #37: a late native refusal answers only its own ask, across reconnects."""
    service, tasks, sent, parent = policy
    connections, capture, ids, at, packets = {}, None, {}, {}, {}
    try:
        for step in case["steps"]:
            op = step["op"]
            if op == "ask":
                number = step["connection"]
                if number not in connections:
                    assert number == len(connections) + 1, "connections open in order"
                    connections[number] = peer(service)
                    if capture is None:
                        capture = stand_in(service, parent, connections[number])
                browser = connections[number]
                assert not browser.closed, "a closed connection never reopens"
                packet = ask_on(service, tasks, sent, capture, browser)
                ids[step["ask"]] = packet["requestId"]
                at[step["ask"]] = capture.resume_asks[packet["requestId"]]
                packets[step["ask"]] = (browser, packet)
            elif op == "reply":
                browser, packet = packets[step["ask"]]
                answer(service, browser, packet, ok=False, error=step["error"], code=step["code"])
            elif op == "close":
                close(service, connections[step["connection"]])
            elif op == "native":
                refuse_native(service, capture, requestId=ids[step["ask"]])
            elif op == "nativeWithoutId":
                refuse_native(service, capture)
            elif op == "forget":
                # GNOME has no ask timeout; its RESUME_ASKS bound forgets the same way.
                capture.clear_resume_ask(ids[step["ask"]])
            else:
                assert op == "nativeUnknownId"
                refuse_native(service, capture, requestId=str(uuid.uuid4()))
        assert len(set(ids.values())) == len(ids), "every ask has its own id"
        assert all(uuid.UUID(value).version == 4 and str(uuid.UUID(value)) == value for value in ids.values())
        # R82 1b: the native message, or the reply's code, at the ask's own time.
        entry = VECTORS["askAttribution"]["entry"]
        expected = [dict(atMs=at[item["ask"]], **entry, message=item["message"]) for item in case["expect"]["entries"]]
        assert resume_refusals(capture) == expected
        assert list(capture.resume_asks) == [ids[name] for name in case["expect"]["pending"]]
    finally:
        if capture is not None:
            capture.stop()


def test_a_native_refusal_on_the_audio_socket_is_keyed_on_its_request_id(wire):  # noqa: F811
    from test_capture_record import declare, pcm
    from test_resume_capture_host import ACK, lose, proxy, resume, started

    client, capture = started(wire)
    try:
        assert client.handle(declare("guest", "doc-1:source:1")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        lose(wire, client, capture)
        capture.mark_resume_ask("ask-A")
        capture.mark_resume_ask("ask-B")
        at_a, at_b = capture.resume_asks["ask-A"], capture.resume_asks["ask-B"]
        attempt = proxy(wire)
        try:
            reply = attempt.handle(resume(capture, timelineOriginMs=capture.origin_ms + 1, requestId="ask-A"))
            assert reply["type"] == "error" and reply["message"] == NATIVE
        finally:
            attempt.close()
        saved = json.loads((capture.directory / "session.json").read_text())
        assert [r for r in saved["refusals"] if r.get("detail") == "resumeRefused"] == [
            dict(atMs=at_a, side="browser", code="startRejected", detail="resumeRefused", message=NATIVE)
        ]
        assert capture.resume_asks == {"ask-B": at_b}
    finally:
        capture.stop()


@pytest.mark.parametrize(
    "request_id",
    ["", "x" * 129, True, -1, 1.5, 2**53, None],
    ids=["empty", "129-code-points", "bool", "negative", "float", "unsafe", "null"],
)
def test_a_malformed_resume_request_id_is_an_invalid_resume(wire, request_id):  # noqa: F811
    from test_resume_capture_host import lose, proxy, resume, started

    client, capture = started(wire)
    try:
        lose(wire, client, capture)
        capture.mark_resume_ask("ask-A")
        before = dict(capture.resume_asks)
        attempt = proxy(wire)
        try:
            # Otherwise a valid resume: only the requestId refuses it.
            reply = attempt.handle(dict(resume(capture), requestId=request_id))
            assert reply["type"] == "error" and reply["message"] == "Invalid resume request"
        finally:
            attempt.close()
        assert not capture.audio_connected and capture.resume_asks == before
        assert "refusals" not in json.loads((capture.directory / "session.json").read_text())
    finally:
        capture.stop()


@pytest.mark.parametrize("request_id", ["é" * 128, 0, 2**53 - 1], ids=["128-code-points", "zero", "max-safe"])
def test_a_valid_resume_request_id_is_accepted(wire, request_id):  # noqa: F811
    from test_resume_capture_host import lose, proxy, resume, started

    client, capture = started(wire)
    try:
        lose(wire, client, capture)
        attempt = proxy(wire)
        try:
            assert attempt.handle(resume(capture, requestId=request_id))["resumed"] is True
        finally:
            attempt.close()
    finally:
        capture.stop()


def test_a_resume_result_after_stop_writes_nothing(policy):  # noqa: F811
    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = stand_in(service, parent, browser)
    ticks(service, tasks, browser)
    (packet,) = asks(sent)
    capture.stop()
    answer(service, browser, packet, ok=False, code="gone")
    saved = json.loads((capture.directory / "session.json").read_text())
    assert not [r for r in saved.get("refusals", []) if r.get("detail") == "resumeRefused"]


def test_the_d52_5_loss_ask_records_its_refusal_too(policy):  # noqa: F811
    from test_browser_resume_policy import recording, released

    service, tasks, sent, parent = policy
    browser = peer(service)
    capture = recording(service, parent, browser)
    try:
        capture.close()
        released(service, browser)
        run(tasks)
        (packet,) = asks(sent)
        answer(service, browser, packet, ok=False, error="Recording could not start.")
        (item,) = resume_refusals(capture)
        assert "message" not in item and item["side"] == "browser"
        # On system/lost the live line stays Why? (no browser line from it).
        assert refusals.live_lines(capture.host.session.manifest["refusals"], [("system", None, 10)]) == []
    finally:
        capture.stop()


def test_resume_refused_never_drives_a_live_line():
    start = refusals.from_result(NO_SOURCES)
    later = refusals.entry(5000, "browser", "startRejected", detail="resumeRefused", message="gone")
    lines = refusals.live_lines([start, later], [("system", None, 0)])
    assert [(line["id"], line["caption"]) for line in lines] == [
        ("errors.othersFallback.line", "errors.refusal.noSources")
    ]
    assert refusals.live_lines([later], [("system", None, 0)]) == []


# 1d. The coverage rule.


def test_a_side_covered_by_a_later_track_shows_no_line():
    others = refusals.entry(0, "others", "deviceUnavailable")
    browser = refusals.from_result(NO_SOURCES)
    # The callConnected seam: browser audio records the others now.
    assert refusals.live_lines([others], [("remote", None, 900)]) == []
    # Started after the refusal, ended since: still covered.
    assert refusals.live_lines([others], [("remote", True, 900)]) == []
    # A later system/lost brings back Why? (the plain fallback), not the start caption.
    assert refusals.live_lines([browser], [("remote", True, 900), ("system", None, 5000)]) == []
    # Before the seam the start refusal's line and caption show.
    assert [line["caption"] for line in refusals.live_lines([browser], [("system", None, 0)])] == [
        "errors.refusal.noSources"
    ]
    # A track that started before the refusal and ended does not cover it.
    me = refusals.entry(5000, "me", "deviceUnavailable")
    assert [line["side"] for line in refusals.live_lines([me], [("microphone", True, 0)])] == ["me"]


# 1c. Try again on a refused native side of a browser capture.


def browser_capture(tmp_path, refuse, *, others_source="browser"):
    from test_native_sides import Taps

    from cassini_talk.browser_capture import BrowserCapture

    taps = Taps(refuse=refuse)
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=taps)
    message = dict(type="start", captureId="retry", timelineOriginMs=int(time.time() * 1000), microphone=True)
    if others_source == "system":
        capture.browser_lost = True
        capture.start(message, others_source="system", reason="missing")
    else:
        capture.start(message)
    return capture, taps


def test_a_retry_reopens_a_refused_microphone_without_a_selection_event(tmp_path):
    capture, taps = browser_capture(tmp_path, {"microphone": 1})
    try:
        events = list(capture.host.session.manifest["events"])
        capture.select(True, True, retry=True)
        assert "microphone" in capture.native_taps and "microphone" not in capture.refused_sides
        assert capture.host.session.manifest["events"] == events, "a retry writes no selection event"
        assert [t["kind"] for t in capture.host.session.manifest["tracks"]] == ["microphone"]
        assert len(capture.host.session.manifest["refusals"]) == 1  # Only the start's.
    finally:
        capture.stop()


def test_a_failing_retry_adds_one_refusal(tmp_path):
    capture, _taps = browser_capture(tmp_path, {"microphone": 2})
    try:
        with pytest.raises(OSError):
            capture.select(True, True, retry=True)
        stored = capture.host.session.manifest["refusals"]
        assert [(r["side"], r["atMs"] > 0) for r in stored] == [("me", False), ("me", True)]
        assert "microphone" in capture.refused_sides and "microphone" not in capture.native_taps
    finally:
        capture.stop()


def test_a_retry_without_a_refused_side_is_a_no_op(tmp_path):
    capture, taps = browser_capture(tmp_path, {})
    try:
        before = (json.dumps(capture.host.session.manifest), len(taps.made))
        capture.select(True, True, retry=True)
        assert (json.dumps(capture.host.session.manifest), len(taps.made)) == before
    finally:
        capture.stop()


def test_a_retry_reopens_a_refused_stand_in_system_tap(tmp_path):
    capture, _taps = browser_capture(tmp_path, {"system": 1}, others_source="system")
    try:
        events = list(capture.host.session.manifest["events"])
        capture.select(True, True, retry=True)
        assert set(capture.native_taps) == {"microphone", "system"}
        assert capture.host.session.manifest["events"] == events
    finally:
        capture.stop()


def test_two_concurrent_retries_open_one_track(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    gate = threading.Event()
    capture, taps = browser_capture(tmp_path, {"microphone": 1})
    taps.gate = {"microphone": gate}  # The retry's tap opens only when released.
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(capture.select, True, True, retry=True)
            second = pool.submit(capture.select, True, True, retry=True)
            gate.set()
            first.result(5), second.result(5)
        assert [t["kind"] for t in capture.host.session.manifest["tracks"]] == ["microphone"]
        assert len([tap for tap in taps.made if tap.kind == "microphone"]) == 2  # The start's, and one retry.
    finally:
        capture.stop()


def test_the_selection_request_carries_retry_to_the_capture(served, tmp_path):  # noqa: F811
    from cassini_talk.browser_service import request

    service, path, _library = served
    capture, _taps = browser_capture(tmp_path, {"microphone": 1})
    service.captures[capture.capture_id] = capture
    try:
        result = request("selection", path=path, directory=str(capture.directory), me=True, others=True, retry=True)
        assert result.get("error") is None
        assert "microphone" in capture.native_taps
    finally:
        capture.stop()


# §2. D17: one line per failed meeting, with its own Try again.


class Peer:
    """A companion with call generations, answering each Start by tab."""

    def __init__(self, path, rows, instance="d17-browser", capabilities=("independent-sessions-v1",)):
        from test_browser_service import wait_for

        from cassini_talk.browser_service import connect, request, send_packet

        self.path, self.sessions = path, {}
        self.connection = connect(path, timeout=5)
        self.stream = self.connection.makefile("rwb")
        send_packet(self.stream, dict(role="extension", browserInstance=instance))
        send_packet(self.stream, dict(type="hello", version=1, capabilities=list(capabilities)))
        send_packet(self.stream, dict(type="status", recording=False, sessions=[]))
        self.meetings(rows)
        wait_for(lambda: len(request(path=path)["meetings"]) == len(rows))

    def meetings(self, rows):
        from cassini_talk.browser_service import send_packet

        send_packet(self.stream, dict(type="meetings", meetings=rows))

    def receive(self):
        from cassini_talk.browser_service import read_packet

        return read_packet(self.stream)

    def answer(self, command, **result):
        from uuid import uuid4

        from cassini_talk.browser_service import send_packet

        tab = command["tabId"]
        if result.get("ok"):
            self.sessions[tab] = dict(recording=True, tabId=tab, captureId=str(uuid4()), path="/private/R-" + str(tab))
        value = self.sessions.get(tab, dict(tabId=tab, recording=False))
        status = dict(value, sessions=list(self.sessions.values()))
        send_packet(self.stream, dict(type="result", requestId=command["requestId"], status=status, **result))

    def close(self):
        self.stream.close()
        self.connection.close()


def meeting_row(tab, generation=None, title="Weekly sync"):
    return dict(
        tabId=tab,
        title=title,
        url=f"https://meet.example/{tab}",
        sourceCount=2,
        callGeneration=generation or f"call-{tab}",
    )


def start_all(path, peer, answers, **values):
    """start-all in a thread; answer each Start by tab; return the snapshot."""
    from concurrent.futures import ThreadPoolExecutor

    from cassini_talk.browser_service import request

    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(request, "start-all", path=path, **values)
        for _ in answers:
            command = peer.receive()
            assert command["action"] == "start" and command["tabId"] in answers, command
            peer.answer(command, **answers[command["tabId"]])
        return pending.result(10)


BUSY = dict(ok=False, error="A recording is already running. Stop it before starting another.", code="busy")


def meeting_id(path, tab):
    from cassini_talk.browser_service import request

    return next(m["id"] for m in request(path=path)["meetings"] if m["tabId"] == tab)


def test_a_partial_start_lists_each_failed_meeting_and_only_retries_that_call(served):  # noqa: F811
    from cassini_talk.browser_service import request

    service, path, library = served
    peer = Peer(path, [meeting_row(3), meeting_row(4)])
    try:
        snapshot = start_all(path, peer, {3: dict(ok=True), 4: BUSY})
        (failure,) = snapshot["failures"]
        assert (failure["callGeneration"], failure["title"]) == ("call-4", "Weekly sync")
        assert failure["refusals"][0]["detail"] == "busy"
        assert not folders(library), "busy never falls back"
        # Try again names that meeting and its call: only it is started.
        only = dict(id=failure["id"], callGeneration="call-4")
        again = start_all(path, peer, {4: dict(NO_SOURCES)}, only=only, groupId="retry-1")
        assert again["attempted"] == 1 and not again["failures"] and not again["cancelled"]
        # A non-busy rejection falls back into that meeting's folder; the line clears.
        (folder,) = folders(library)
        shown = {m["tabId"]: m for m in request(path=path)["meetings"]}
        assert shown[4]["recording"] is True
        # Exactly one Recording-* folder per meeting, whatever is retried.
        assert start_all(path, peer, {}, only=only)["attempted"] == 0
        assert folders(library) == [folder]
    finally:
        peer.close()


def test_a_retry_starts_nothing_for_a_changed_call_or_a_call_joined_since(served):  # noqa: F811
    service, path, library = served
    peer = Peer(path, [meeting_row(3), meeting_row(4)])
    try:
        snapshot = start_all(path, peer, {3: dict(ok=True), 4: BUSY})
        (failure,) = snapshot["failures"]
        # A new call in that tab, and a call joined since the first Record.
        peer.meetings([meeting_row(3), meeting_row(4, "call-4b"), meeting_row(5)])
        from test_browser_service import wait_for

        from cassini_talk.browser_service import request

        wait_for(lambda: len(request(path=path)["meetings"]) == 3)
        result = start_all(path, peer, {}, only=dict(id=failure["id"], callGeneration="call-4"))
        assert result["attempted"] == 0 and not folders(library)
    finally:
        peer.close()


def test_a_reconnect_with_a_new_call_generation_starts_nothing(served):  # noqa: F811
    service, path, library = served
    first = Peer(path, [meeting_row(3), meeting_row(4)])
    snapshot = start_all(path, first, {3: dict(ok=True), 4: BUSY})
    (failure,) = snapshot["failures"]
    first.close()
    from test_browser_service import wait_for

    wait_for(lambda: not service.browsers)
    returning = Peer(path, [meeting_row(4, "call-4b")])
    try:
        assert meeting_id(path, 4) == failure["id"], "ids are stable across reconnects"
        result = start_all(path, returning, {}, only=dict(id=failure["id"], callGeneration="call-4"))
        assert result["attempted"] == 0
    finally:
        returning.close()


def test_stop_all_cancels_a_retry(served):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor

    from cassini_talk.browser_service import request

    service, path, _library = served
    peer = Peer(path, [meeting_row(4)])
    try:
        only = dict(id=meeting_id(path, 4), callGeneration="call-4")
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path, only=only, groupId="retry-2")
            assert peer.receive()["action"] == "start"  # The extension never answers.
            request("stop-all", path=path, cancelGroups=["retry-2"])
            result = pending.result(10)
        assert result["cancelled"] is True
    finally:
        peer.close()


MULTI = ("independent-sessions-v1", "multi-stream-v1")
GONE = dict(ok=False, error="That call is no longer active.", code="gone")


def generation(service, tab):
    return next(b.meetings[tab].get("callGeneration") for b in service.browsers.values() if tab in b.meetings)


def sent_starts(service, monkeypatch):
    """Every Start packet any connection queues from now on."""
    starts = []
    for browser in service.browsers.values():

        def send(packet, original=browser.send):
            if packet.get("action") == "start":
                starts.append(packet)
            return original(packet)

        monkeypatch.setattr(browser, "send", send)
    return starts


def rejoined(peer, service):
    from test_browser_service import wait_for

    peer.meetings([meeting_row(3), meeting_row(4, "call-4b")])
    wait_for(lambda: generation(service, 4) == "call-4b")


def test_a_retry_never_starts_a_call_rejoined_before_its_start(served, monkeypatch):  # noqa: F811
    from cassini_talk.browser_service import request

    service, path, library = served
    peer = Peer(path, [meeting_row(3), meeting_row(4)], capabilities=MULTI)
    try:
        (failure,) = start_all(path, peer, {3: dict(ok=True), 4: BUSY})["failures"]
        recorded = folders(library)
        original = service.command

        def late(identifier, action, *args, **kwargs):
            if action == "start":
                rejoined(peer, service)  # G2 lands between the target filter and command().
            return original(identifier, action, *args, **kwargs)

        monkeypatch.setattr(service, "command", late)
        starts = sent_starts(service, monkeypatch)
        result = request("start-all", path=path, only=dict(id=failure["id"], callGeneration="call-4"), groupId="r")
        assert result["attempted"] == 1 and starts == [], "no Start for the rejoined call"
        (again,) = result["failures"]
        assert (again["id"], again["callGeneration"]) == (failure["id"], "call-4")
        assert again["error"] == "This meeting is no longer available"
        assert folders(library) == recorded, "no Recording-* folder for the rejoined call"
    finally:
        peer.close()


def test_a_retry_never_falls_back_into_a_call_rejoined_during_its_start(served, monkeypatch):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor

    from cassini_talk.browser_service import request

    service, path, library = served
    peer = Peer(path, [meeting_row(3), meeting_row(4)], capabilities=MULTI)
    try:
        (failure,) = start_all(path, peer, {3: dict(ok=True), 4: BUSY})["failures"]
        before, recorded = set(service.captures), folders(library)
        original = service.start_fallback

        def late(*args, **kwargs):
            rejoined(peer, service)  # G2 lands during the Start's round trip.
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "start_fallback", late)
        with ThreadPoolExecutor(1) as pool:
            only = dict(id=failure["id"], callGeneration="call-4")
            pending = pool.submit(request, "start-all", path=path, only=only, groupId="r")
            command = peer.receive()
            assert command["action"] == "start" and command["callGeneration"] == "call-4"
            peer.answer(command, **GONE)  # Not busy: GNOME would fall back.
            result = pending.result(10)
        (again,) = result["failures"]
        assert (again["id"], again["callGeneration"]) == (failure["id"], "call-4")
        assert set(service.captures) == before, "no stand-in for the rejoined call"
        assert folders(library) == recorded, "no Recording-* folder for the rejoined call"
    finally:
        peer.close()


def fake_app(application, **values):
    from types import SimpleNamespace

    defaults = dict(
        _record_start_future=object(),
        _record_start_group="group",
        _record_start_generation=1,
        _quitting=False,
        _capture_exiting=False,
        recording_failure=None,
        recording_failures=[],
        recording_error=None,
        _pending_recording=None,
        start_native=lambda: pytest.fail("No native start"),
        get_windows=lambda: [],
        _native_changed=lambda: None,
        _show_pending_recording=lambda: None,
        release=lambda: None,
        failure_entry=application.failure_entry,
    )
    app = SimpleNamespace(**dict(defaults, **values))
    app._browser_changed = lambda snapshot, _received_at=None: application._prune_recording_failures(app, snapshot)
    return app


def finish(application, app, snapshot, retried=None):
    from concurrent.futures import Future

    future = Future()
    future.set_result(snapshot)
    application._recordings_started(app, future, 1, retried)


def partial(meetings=(), **failure):
    busy = dict(atMs=0, side="browser", code="startRejected", detail="busy", message="A recording is already running.")
    return dict(
        cancelled=False,
        statuses=[dict(recording=True, path="/library/Recording-a", captureId="a")],
        meetings=list(meetings),
        failures=[
            dict(
                dict(id="call/4", title="Weekly sync", callGeneration="call-4", error="raw text", refusals=[busy]),
                **failure,
            )
        ],
    )


def test_a_partial_start_gives_one_line_per_failed_meeting_and_no_raw_text(application_class):  # noqa: F811
    app = fake_app(application_class)
    finish(application_class, app, partial([dict(id="call/4", callGeneration="call-4", recording=False)]))
    assert app.recording_failures == [
        dict(id="call/4", callGeneration="call-4", title="Weekly sync", caption="errors.refusal.busy")
    ]
    assert app.recording_error is None and app.recording_failure is None
    blank = fake_app(application_class)
    finish(application_class, blank, partial([dict(id="call/4", callGeneration="call-4")], title="  "))
    assert blank.recording_failures[0]["title"] is None  # Shown as library.untitled.


def test_a_retry_replaces_keeps_or_clears_its_line(application_class):  # noqa: F811
    entry = dict(id="call/4", callGeneration="call-4", title="Weekly sync", caption="errors.refusal.busy")
    present = [dict(id="call/4", callGeneration="call-4", recording=False)]
    # A new failure replaces the entry; never the banner, never a native start.
    app = fake_app(application_class, recording_failures=[dict(entry)])
    no_answer = [dict(atMs=0, side="browser", code="startFailed", detail="timeout")]
    retried = dict(partial(present, refusals=no_answer), statuses=[])
    finish(application_class, app, retried, retried=entry)
    assert app.recording_failures == [dict(entry, caption="errors.refusal.noAnswer")]
    assert app.recording_failure is None
    # Attempted 0 (nothing started, nothing failed) keeps the line.
    app = fake_app(application_class, recording_failures=[dict(entry)])
    finish(application_class, app, dict(cancelled=False, statuses=[], meetings=present, failures=[]), retried=entry)
    assert app.recording_failures == [entry]
    # Its call records now, leaves, or changes generation: the line clears.
    for meetings in (
        [dict(id="call/4", callGeneration="call-4", recording=True)],
        [],
        [dict(id="call/4", callGeneration="call-4b", recording=False)],
    ):
        app = fake_app(application_class, recording_failures=[dict(entry)])
        finish(
            application_class, app, dict(cancelled=False, statuses=[], meetings=meetings, failures=[]), retried=entry
        )
        assert app.recording_failures == [], meetings


def test_every_distinct_caption_shows_when_several_meetings_fail():
    busy = [refusals.entry(0, "browser", "startRejected", detail="busy")]
    none = [refusals.entry(0, "browser", "startRejected")]
    no_answer = [refusals.entry(0, "browser", "startFailed", detail="timeout")]
    banner = refusals.banner(busy, none, no_answer, busy)
    assert banner["captions"] == ["errors.refusal.busy", "errors.refusal.noAnswer"]
    assert banner["caption"] == "errors.refusal.busy" and banner["refusals"] == busy


def test_a_native_retry_writes_no_equal_valued_selection(tmp_path):
    from test_native_sides import Sides
    from test_recorder import spin_until

    from cassini_talk.native_recorder import NativeRecorder

    recorder = NativeRecorder()
    sides = Sides(recorder, {"microphone": ("fail", 0), "system": ("ok", 0.1)})
    recorder.start(tmp_path, test=True)
    try:
        spin_until(lambda: not recorder.starting)
        events = list(recorder.session.manifest["events"])
        sides.plan.pop("microphone")  # The device works now.
        future = recorder.select(True, True)  # Try again: the selection is unchanged.
        spin_until(lambda: future.done() and not recorder._changing_sources)
        future.result()
        assert "microphone" in recorder._track_ids
        assert recorder.session.manifest["events"] == events, "no selection or othersSource event"
    finally:
        recorder.stop()
        spin_until(lambda: recorder.pipeline is None and not recorder._finishing)
        recorder._executor.shutdown(wait=True)


def test_try_again_on_a_browser_capture_sends_selection_with_retry(application_class, monkeypatch):  # noqa: F811
    from concurrent.futures import Future
    from pathlib import Path
    from types import SimpleNamespace

    from cassini_talk import browser_service

    sent = []
    monkeypatch.setattr(browser_service, "request", lambda action, **values: sent.append((action, values)) or {})

    class Executor:
        def submit(self, function, *args, **kwargs):
            future = Future()
            future.set_result(function(*args, **kwargs))
            return future

    sources = SimpleNamespace(me=True, others=True)
    app = SimpleNamespace(
        _quitting=False,
        _capture_exiting=False,
        _source_changes={},
        native_recorder_for=lambda _directory: None,
        comfort=SimpleNamespace(sources=lambda _meeting: sources),
        hold=lambda: None,
        release=lambda: None,
        _record_start_executor=Executor(),
        _native_changed=lambda: None,
        _recording_sources_changed=lambda *_: False,
    )
    meeting = SimpleNamespace(directory=Path("/library/Recording-x"))
    application_class.change_recording_sources(app, meeting, True, True, retry=True)
    assert sent == [("selection", dict(directory="/library/Recording-x", me=True, others=True, retry=True))]
    sent.clear()
    app._source_changes.clear()
    application_class.change_recording_sources(app, meeting, True, False)
    assert sent == [("selection", dict(directory="/library/Recording-x", me=True, others=False))]


def test_a_meeting_retry_waits_for_a_pending_start(application_class, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    started = []
    app = SimpleNamespace(
        _record_start_future=object(),  # A start is pending.
        _quitting=False,
        _record_start_executor=SimpleNamespace(submit=lambda *a, **k: started.append(a)),
    )
    application_class.retry_meeting(app, dict(id="call/4", callGeneration="call-4"))
    assert not started
