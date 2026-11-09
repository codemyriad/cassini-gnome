# SPDX-License-Identifier: GPL-3.0-or-later
"""D80 consumer proof: actual broker decisions, final dispatch and lease clocks."""

import copy
import io
import json
from pathlib import Path

import pytest
from test_browser_capture import server  # noqa: F401

from cassini_talk.browser_service import BrowserConnection, BrowserService
from cassini_talk.call_admission import CAPABILITIES, LEASE

VECTORS = json.loads(
    (Path(__file__).parents[1] / "browser-protocol/conformance/control/call-signals-vectors.json").read_text()
)
POSITIVE = next(c for c in VECTORS["routing"] if c["name"] == "separated-existing-rtp-ready")


@pytest.fixture
def broker(tmp_path, monkeypatch):
    service = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    peer = BrowserConnection(io.BytesIO(), browser_instance="private-browser")
    service.browsers[peer.id] = peer
    clock, queued, sent = [0.0], [], []
    monkeypatch.setattr(service, "clock", lambda: clock[0])
    monkeypatch.setattr(service.workers, "submit", lambda fn, *args: queued.append((fn, args)))

    def send(packet):
        sent.append(packet)
        if packet.get("action") == "start":
            service.update(
                peer,
                dict(type="result", requestId=packet["requestId"], ok=False, code="busy", error="no capture in proof"),
            )

    monkeypatch.setattr(peer, "send", send)
    service.update(
        peer,
        dict(
            type="hello",
            version=1,
            capabilities=sorted(
                CAPABILITIES | {"independent-sessions-v1", "call-evidence-v1", "page-state-v1", "multi-stream-v1"}
            ),
        ),
    )
    service.update(peer, dict(type="configured", capabilities=sorted(CAPABILITIES)))
    service.update(peer, dict(type="status", recording=False))
    meeting = copy.deepcopy(POSITIVE["rtp"])
    meeting.update(title="Controlled generic call", url="https://conference.invalid/session")
    service.update(peer, dict(type="meetings", meetings=[meeting]))
    yield service, peer, clock, queued, sent, meeting
    service.closed.set()
    for owner in {peer, *service.browsers.values()}:
        owner.shutdown()
        if owner.writer is not None:
            owner.writer.join(2)
            assert not owner.writer.is_alive(), "private control writer did not stop"
    service.workers.shutdown(wait=True)


def report(service, peer, *, sequence=0, remote="separated", state="active"):
    packet = copy.deepcopy(POSITIVE["signal"])
    packet["sequence"] = sequence
    row = packet["tabs"][0]
    row["remoteAudio"] = remote
    if state != "active":
        row["callEvidence"].update(state=state, reason="unverified", callGeneration=None)
    service.update(peer, packet)


def starts(sent):
    return [p for p in sent if p.get("action") == "start"]


def test_ack_and_first_correlated_proof_are_required(broker):
    service, peer, _clock, queued, sent, _meeting = broker
    service.auto_record = True
    service.policy()
    assert not queued and not starts(sent)
    report(service, peer)
    assert len(queued) == 1
    fn, args = queued.pop(0)
    fn(*args)
    assert len(starts(sent)) == 1


def test_final_dispatch_expires_at_exact_lease_deadline(broker, monkeypatch):
    service, peer, clock, _queued, sent, _meeting = broker
    report(service, peer)
    service.auto_record = True
    original = service._validate_generation
    checks = []

    def validate(*args):
        original(*args)
        checks.append(True)
        if len(checks) == 2:
            clock[0] = LEASE

    monkeypatch.setattr(service, "_validate_generation", validate)
    with pytest.raises(ValueError, match="Verified call evidence"):
        service.command(f"{peer.id}:2", "start", automatic=True)
    assert not starts(sent)


@pytest.mark.parametrize("field", ["browserDocumentId", "documentGeneration", "callGeneration"])
def test_identity_change_cancels_queued_start(broker, field):
    service, peer, _clock, queued, sent, meeting = broker
    report(service, peer)
    service.auto_record = True
    service.policy()
    fn, args = queued.pop(0)
    meeting[field] = "replacement"
    service.update(peer, dict(type="meetings", meetings=[meeting]))
    fn(*args)
    assert not starts(sent)


def test_browser_rejection_after_proof_loss_cannot_reserve_fallback(broker, monkeypatch):
    service, peer, _clock, queued, sent, _meeting = broker
    report(service, peer)
    service.auto_record = True
    service.policy()
    fallback = []
    monkeypatch.setattr(service, "start_fallback", lambda *args, **kwargs: fallback.append(args))

    def refuse(packet):
        if packet.get("action") == "start":
            sent.append(packet)
            report(service, peer, sequence=1, state="unknown")
            service.update(
                peer, dict(type="result", requestId=packet["requestId"], ok=False, code="unsupported", error="refused")
            )

    monkeypatch.setattr(peer, "send", refuse)
    fn, args = queued.pop(0)
    fn(*args)
    assert len(starts(sent)) == 1 and fallback == []


@pytest.mark.parametrize("case", VECTORS["freshness"]["cases"], ids=lambda c: c["name"])
def test_shared_receipt_time_freshness(broker, case):
    service, peer, clock, _queued, _sent, meeting = broker
    for step in case["steps"]:
        clock[0] = step["atMs"] / 1000
        event = step["event"]
        if event in ("active", "replayed-active"):
            report(service, peer, sequence=step["sequence"])
        elif event == "configured":
            service._configure_signals(peer, step["capabilities"])
            service.update(peer, dict(type="configured", capabilities=step["capabilities"]))
        elif event in ("unknown", "inactive"):
            report(service, peer, sequence=step.get("sequence", peer.signals_sequence + 1), state="unknown")
        elif event in ("pong", "status"):
            service.update(peer, dict(type=event, recording=False))
        elif event in ("meetings", "meetings-heard"):
            service.update(peer, dict(type="meetings", meetings=[meeting]))
        elif event == "other-tab-active":
            # An unrelated controller/profile earns its own proof. It cannot
            # renew this row's receipt-time lease through a global timestamp.
            other = BrowserConnection(io.BytesIO(), browser_instance="other-private-browser")
            service.browsers[other.id] = other
            other.send = lambda _packet: None
            service.update(other, dict(type="hello", version=1, capabilities=sorted(CAPABILITIES)))
            service.update(other, dict(type="configured", capabilities=sorted(CAPABILITIES)))
            packet = copy.deepcopy(POSITIVE["signal"])
            packet["browserInstance"] = "other-profile"
            packet["tabs"][0]["tabId"] = 9
            service.update(other, packet)
        # attempt-active-refresh is a producer-rejected operation, not an
        # accepted wire row: counters never cross the consumer protocol.
        service.policy()
        if "consumerFresh" in step["expect"]:
            assert service._auto_start_ready(peer, meeting) is step["expect"]["consumerFresh"], step


def test_actual_writer_drops_start_held_until_proof_expiry(broker, monkeypatch):
    import threading

    service, peer, clock, _queued, _sent, _meeting = broker
    report(service, peer)
    service.auto_record = True
    entered, release, queued, finished = (threading.Event() for _ in range(4))
    written, errors = [], []

    class Stream:
        def write(self, data):
            if not entered.is_set():
                entered.set()
                assert release.wait(2), "test must release the private writer"
            written.append(json.loads(data))

        def flush(self):
            pass

    peer.stream = Stream()
    real_send = BrowserConnection.send

    def send(packet):
        real_send(peer, packet)
        if packet.get("action") == "start":
            queued.set()

    monkeypatch.setattr(peer, "send", send)
    peer.send(dict(type="heldPriorWrite"))
    assert entered.wait(2)

    def command():
        try:
            service.command(f"{peer.id}:2", "start", automatic=True)
        except ValueError as error:
            errors.append(str(error))
        finally:
            finished.set()

    worker = threading.Thread(target=command)
    worker.start()
    try:
        assert queued.wait(2)
        clock[0] = LEASE
        release.set()
        assert finished.wait(2)
        assert errors == ["Recording start was cancelled"]
        assert not starts(written)
    finally:
        release.set()
        peer.shutdown()
        worker.join(2)
        if peer.writer:
            peer.writer.join(2)
        assert not worker.is_alive() and not peer.writer.is_alive()


def test_input_factory_revalidates_after_parent_admission(broker, tmp_path, monkeypatch):
    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.refusals import StartCancelled

    service, peer, clock, _queued, _sent, meeting = broker
    report(service, peer)
    service.auto_record = True
    proof = service.signal_calls.proof(peer, 2, clock[0])
    opened = []
    monkeypatch.setattr(service, "tap_factory", lambda *args, **kwargs: opened.append(args))
    monkeypatch.setattr(service, "capture_parent", tmp_path / "Library")
    original = BrowserCapture._open_natives

    def expire(capture, kinds, **kwargs):
        clock[0] = LEASE
        return original(capture, kinds, **kwargs)

    monkeypatch.setattr(BrowserCapture, "_open_natives", expire)
    with pytest.raises(StartCancelled):
        service.start_fallback(
            f"{peer.id}:2", meeting, automatic_generation=service._stop_generation, verified_binding=proof.binding
        )
    assert opened == []


def test_native_start_after_automatic_command_cannot_open_on_expired_proof(broker, monkeypatch):
    from cassini_talk.refusals import StartCancelled

    service, peer, clock, _queued, _sent, meeting = broker
    report(service, peer)
    service.auto_record = True

    def accept(packet):
        if packet.get("action") == "start":
            service.update(
                peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
            )

    monkeypatch.setattr(peer, "send", accept)
    service.command(f"{peer.id}:2", "start", automatic=True)
    clock[0] = LEASE
    with pytest.raises(StartCancelled, match="expired"):
        service._native_start_guard(
            dict(
                requestId=next(reversed(service._native_requests)),
                captureId="late-native",
                callGeneration=meeting["callGeneration"],
                documentGeneration=meeting["documentGeneration"],
            )
        )
    assert not service.captures


def test_independent_start_cannot_infer_manual_intent_from_automatic_history(broker):
    from cassini_talk.browser_service import StartRejected
    from cassini_talk.refusals import StartCancelled

    service, peer, clock, _queued, _sent, meeting = broker
    report(service, peer)
    service.auto_record = True
    with pytest.raises(StartRejected):
        service.command(f"{peer.id}:2", "start", automatic=True)
    clock[0] = LEASE
    with pytest.raises(StartRejected):
        service.command(f"{peer.id}:2", "start", user=True)
    with pytest.raises(StartCancelled, match="requestId"):
        service._native_start_guard(
            dict(
                captureId="explicit-manual",
                callGeneration=meeting["callGeneration"],
                documentGeneration=meeting["documentGeneration"],
            )
        )


@pytest.mark.parametrize("case", VECTORS["qualificationMeaning"]["cases"], ids=lambda c: c["name"])
def test_generic_qualification_is_consumed_without_hostname_or_intent_heuristics(broker, case):
    service, peer, _clock, queued, sent, _meeting = broker
    service.auto_record = True
    report(service, peer, state=case["expectState"])
    while queued:
        fn, args = queued.pop(0)
        fn(*args)
    assert bool(starts(sent)) is (case["expectState"] == "active")


def test_new_fresh_binding_can_schedule_after_cancelling_old_queued_binding(broker):
    service, peer, _clock, queued, sent, meeting = broker
    report(service, peer)
    service.auto_record = True
    service.policy()
    old, args = queued.pop(0)
    meeting.update(browserDocumentId="chrome-replacement", documentGeneration="hook-replacement")
    service.update(peer, dict(type="meetings", meetings=[meeting]))
    packet = copy.deepcopy(POSITIVE["signal"])
    packet["sequence"] = 1
    packet["tabs"][0].update(
        documentGeneration=meeting["browserDocumentId"], sourceDocumentGeneration=meeting["documentGeneration"]
    )
    service.update(peer, packet)
    old(*args)
    assert not starts(sent) and len(queued) == 1
    new, args = queued.pop(0)
    new(*args)
    assert len(starts(sent)) == 1


def test_unknown_or_replayed_native_request_cannot_create_another_capture(broker, monkeypatch):
    from cassini_talk.refusals import StartCancelled

    service, peer, _clock, _queued, _sent, meeting = broker

    def accept(packet):
        if packet.get("action") == "start":
            service.update(
                peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
            )

    monkeypatch.setattr(peer, "send", accept)
    service.command(f"{peer.id}:2", "start", user=True)
    token = next(reversed(service._native_requests))
    packet = dict(
        type="start",
        captureId="first-manual",
        requestId=token,
        callGeneration=meeting["callGeneration"],
        documentGeneration=meeting["documentGeneration"],
    )
    assert service._native_start_guard(packet) is None
    with pytest.raises(StartCancelled, match="already owns"):
        service._native_start_guard(dict(packet, captureId="second-manual"))
    with pytest.raises(StartCancelled, match="Unknown"):
        service._native_start_guard(dict(packet, requestId="unissued-token"))
    with pytest.raises(StartCancelled, match="requestId"):
        service._native_start_guard(dict(captureId="unrequested-independent"))


def test_stop_all_fences_verified_call_before_legacy_inventory_is_ready(broker):
    service, peer, _clock, queued, sent, meeting = broker
    service.update(peer, dict(type="meetings", meetings=[]))
    report(service, peer)
    service.auto_record = True
    service.stop_all()
    service.update(peer, dict(type="meetings", meetings=[meeting]))
    while queued:
        fn, args = queued.pop(0)
        fn(*args)
    assert not starts(sent), "A verified current call must stay stopped when its RTP inventory arrives later"


def test_stop_all_suppresses_only_current_call_and_new_background_call_can_start(broker):
    service, peer, _clock, queued, sent, meeting = broker
    service.update(peer, dict(type="meetings", meetings=[]))
    report(service, peer)
    service.auto_record = True
    service.stop_all()
    meeting["callGeneration"] = "new-background-call"
    service.update(peer, dict(type="meetings", meetings=[meeting]))
    packet = copy.deepcopy(POSITIVE["signal"])
    packet["sequence"] = 1
    packet["tabs"][0]["callEvidence"]["callGeneration"] = "new-background-call"
    service.update(peer, packet)
    while queued:
        fn, args = queued.pop(0)
        fn(*args)
    assert len(starts(sent)) == 1 and starts(sent)[0]["callGeneration"] == "new-background-call"


@pytest.mark.parametrize("independent", [True, False])
def test_schedule_keeps_the_binding_of_the_eligibility_proof(broker, monkeypatch, independent):
    service, peer, clock, queued, sent, _meeting = broker
    if not independent:
        peer.capabilities -= {"independent-sessions-v1"}
    report(service, peer)
    service.auto_record = True
    original = service.signal_calls.proof

    def expire_after_check(*args, **kwargs):
        proof = original(*args, **kwargs)
        if kwargs.get("meeting") is not None and proof is not None:
            clock[0] = LEASE
        return proof

    monkeypatch.setattr(service.signal_calls, "proof", expire_after_check)
    service.policy()
    assert len(queued) == 1
    function, args = queued.pop(0)
    function(*args)
    assert not starts(sent), "final dispatch must still reject the expired proof"
    monkeypatch.setattr(service.signal_calls, "proof", original)
    clock[0] = LEASE + 1
    report(service, peer, sequence=1)
    assert len(queued) == 1, "a fresh proof must remain eligible after the cancelled Start"
    function, args = queued.pop(0)
    function(*args)
    assert len(starts(sent)) == 1


@pytest.mark.parametrize("loss", ["reload", "workerRestart", "autoOff"])
@pytest.mark.parametrize("fallback", ["audioClose", "policy"])
def test_established_automatic_capture_falls_back_after_start_admission_changes(
    broker, tmp_path, monkeypatch, loss, fallback
):
    import time

    from durable import saved
    from test_browser_capture import Tap, stream

    from cassini_talk.browser_capture import BrowserCapture

    service, peer, clock, queued, _sent, meeting = broker
    report(service, peer)
    service.auto_record = True

    def accept(packet):
        if packet.get("action") in ("start", "stop"):
            service.update(
                peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
            )

    monkeypatch.setattr(peer, "send", accept)
    service.command(f"{peer.id}:2", "start", automatic=True)
    token = next(reversed(service._native_requests))
    packet = dict(
        type="start",
        captureId="established",
        requestId=token,
        timelineOriginMs=int(time.time() * 1000),
        microphone=False,
        callGeneration=meeting["callGeneration"],
        documentGeneration=meeting["documentGeneration"],
    )
    admit = service._native_start_guard(packet)
    capture = BrowserCapture(service._capture_changed, parent=tmp_path / "Library", tap_factory=Tap)
    service.captures[capture_id := packet["captureId"]] = capture
    capture.start(packet, admit=admit)
    service._native_requests[token]["nativeAdmitted"] = True
    assert capture.ready.is_set() and not capture.starting and admit()
    try:
        if loss == "reload":
            meeting["pageState"] = "navigating"
            service.update(peer, dict(type="meetings", meetings=[meeting]))
        elif loss == "workerRestart":
            service.update(peer, dict(type="hello", version=1, capabilities=sorted(peer.capabilities)))
        else:
            service.auto_record = False
        assert not admit(), "the test must withdraw startup eligibility"
        if fallback == "audioClose":
            capture.close()
        else:
            service._capture_lifecycle(capture_id, capture, f"{peer.id}:2", "fallback")
        assert capture.browser_lost and not capture.finished and capture.error is None
        assert stream(capture, "system")["frames"] > 0
        assert capture.host.session.manifest["state"] == "recording"
        if loss != "autoOff":
            clock[0] = LEASE
            service.policy()
            while queued:
                function, args = queued.pop(0)
                function(*args)
            since = service._verified_losses[capture_id]
            clock[0] = since + 119.999
            service.policy()
            assert not capture.stop_requested.is_set()
            clock[0] = since + 120
            service.policy()
            while queued:
                function, args = queued.pop(0)
                function(*args)
            saved(capture)
            assert capture.end_reason == "pageGone" and capture.error is None
    finally:
        if not capture.finished:
            capture.stop(user_stopped=False)
        saved(capture)


@pytest.mark.parametrize("route", ["separated", "mixed"])
@pytest.mark.parametrize("order", ["configured", "rotation"])
@pytest.mark.parametrize("arrival", [4.0, LEASE])
def test_evidence_before_matching_inventory_keeps_its_original_receipt(broker, route, order, arrival):
    service, peer, clock, queued, sent, meeting = broker
    previous = dict(meeting)
    if order == "configured":
        service.update(peer, dict(type="hello", version=1, capabilities=sorted(peer.capabilities)))
        previous.pop("browserDocumentId")
    else:
        meeting = dict(meeting, callGeneration="next-call")
    service.update(peer, dict(type="meetings", meetings=[previous]))
    if order == "configured":
        service.update(peer, dict(type="configured", capabilities=sorted(CAPABILITIES)))
    service.auto_record = True
    packet = copy.deepcopy(POSITIVE["signal"])
    packet["tabs"][0]["remoteAudio"] = route
    packet["tabs"][0]["callEvidence"]["callGeneration"] = meeting["callGeneration"]
    service.update(peer, packet)
    proof = service.signal_calls.proof(peer, 2, clock[0])
    assert proof is not None and proof.received == 0 and proof.expires == LEASE
    assert not queued and not service.captures and not starts(sent), "unmatched inventory must not admit"
    clock[0] = arrival
    service.update(peer, dict(type="meetings", meetings=[meeting]))
    assert len(queued) == (1 if arrival < LEASE else 0)
    current = service.signal_calls.proof(peer, 2, clock[0])
    assert current == (proof if arrival < LEASE else None), "inventory cannot renew the lease"


@pytest.mark.parametrize("change", ["identity", "removed"])
def test_inventory_withdraws_previously_joined_proof_without_reviving_it(broker, change):
    service, peer, clock, queued, _sent, meeting = broker
    report(service, peer)
    assert service.signal_calls.proof(peer, 2, clock[0]) is not None
    replacement = dict(meeting, browserDocumentId="departed-document")
    service.update(peer, dict(type="meetings", meetings=[] if change == "removed" else [replacement]))
    assert service.signal_calls.proof(peer, 2, clock[0]) is None
    service.auto_record = True
    service.update(peer, dict(type="meetings", meetings=[meeting]))
    assert not queued, "an inventory replay cannot revive withdrawn evidence"


@pytest.mark.parametrize("automatic", [False, True])
def test_verified_independent_start_requires_exact_request_id(broker, monkeypatch, automatic):
    from cassini_talk.refusals import StartCancelled

    service, peer, _clock, _queued, _sent, meeting = broker
    report(service, peer)
    service.auto_record = True

    def accept(packet):
        service.update(peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False)))

    monkeypatch.setattr(peer, "send", accept)
    service.command(f"{peer.id}:2", "start", automatic=automatic, user=not automatic)
    with pytest.raises(StartCancelled):
        service._native_start_guard(dict(captureId="unidentified", callGeneration=meeting["callGeneration"]))
    token = next(reversed(service._native_requests))
    assert service._native_requests[token].get("nativeCaptureId") is None


def test_same_request_and_capture_cannot_be_replayed_before_directory_creation(broker, monkeypatch):
    from cassini_talk.refusals import StartCancelled

    service, peer, _clock, _queued, _sent, meeting = broker
    monkeypatch.setattr(
        peer,
        "send",
        lambda packet: service.update(
            peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
        ),
    )
    service.command(f"{peer.id}:2", "start", user=True)
    packet = dict(
        captureId="cancelled-before-directory",
        requestId=next(reversed(service._native_requests)),
        callGeneration=meeting["callGeneration"],
    )
    assert service._native_start_guard(packet) is None
    assert not service.captures
    with pytest.raises(StartCancelled, match="already"):
        service._native_start_guard(packet)


@pytest.mark.parametrize("blocked", ["starting", "policyPending"])
def test_call_ended_is_latched_before_a_later_unknown_snapshot(broker, blocked):
    import threading
    from types import SimpleNamespace

    service, peer, _clock, _queued, _sent, _meeting = broker
    report(service, peer)
    key = "starting-or-busy-owner"
    service._verified_owners[key] = service.signal_calls.proof(peer, 2, 0).binding
    capture = SimpleNamespace(
        finished=False,
        starting=blocked == "starting",
        stopping=False,
        stop_requested=threading.Event(),
        browser_lost=False,
        readiness=lambda **_kwargs: None,
    )
    service.captures[key] = capture
    if blocked == "policyPending":
        service._capture_policy_pending.add(key)
    packet = copy.deepcopy(POSITIVE["signal"])
    packet["sequence"] = 1
    packet["tabs"][0]["callEvidence"].update(state="inactive", reason="callEnded")
    service.update(peer, packet)
    report(service, peer, sequence=2, state="unknown")
    capture.starting = False
    service._capture_policy_pending.discard(key)
    assert service._verified_owner_action(key) == "stop:callEnded"
    assert service._verified_ended[key] == "stop:callEnded"
    service.policy()
    assert any(
        args == (key, capture, None, "stop:callEnded")
        for function, args in _queued
        if function == service._capture_lifecycle
    )


@pytest.mark.parametrize("case", VECTORS["nativeStartCorrelation"]["cases"], ids=lambda c: c["name"])
def test_shared_native_start_correlation_vectors_use_exact_retired_and_manual_asks(broker, monkeypatch, case):
    from cassini_talk.refusals import StartCancelled

    service, peer, _clock, _queued, _sent, _meeting = broker
    report(service, peer)
    service.auto_record = True
    monkeypatch.setattr(
        peer,
        "send",
        lambda packet: service.update(
            peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
        ),
    )
    for index, command in enumerate(case["commands"]):
        service.command(f"{peer.id}:2", command["action"], automatic=index == 0, user=index != 0)
        if command["action"] == "start":
            actual = next(reversed(service._native_requests))
            service._native_requests[command["requestId"]] = service._native_requests.pop(actual)
    accepted, rejected = [], []
    for index in case["nativeArrivalOrder"]:
        packet = dict(case["expectedNativeStarts"][index], captureId=f"vector-capture-{index}")
        try:
            assert service._native_start_guard(packet) is None
            accepted.append(packet["requestId"])
        except StartCancelled:
            rejected.append(packet["requestId"])
    assert accepted == [case["expect"]["acceptManualRequestId"]]
    assert rejected == [case["expect"]["rejectRetiredAutomaticRequestId"]]
    assert not service.captures


@pytest.mark.parametrize("scenario", ["sole", "ambiguous", "verifiedOther", "wrongGeneration", "cancelled"])
def test_legacy_unechoed_start_needs_one_current_unclaimed_manual_ask(broker, monkeypatch, scenario):
    from cassini_talk.refusals import StartCancelled

    service, peer, _clock, _queued, _sent, meeting = broker
    peer.verified_configured = False

    def accept(owner, packet):
        service.update(owner, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False)))

    monkeypatch.setattr(peer, "send", lambda packet: accept(peer, packet))
    service.command(f"{peer.id}:2", "start", user=True)
    if scenario in ("ambiguous", "verifiedOther"):
        other = BrowserConnection(io.BytesIO(), browser_instance="other-manual-profile")
        service.browsers[other.id] = other
        other.capabilities = {"independent-sessions-v1"}
        other.status_seen = True
        other.verified_configured = scenario == "verifiedOther"
        other.meetings[2] = dict(meeting)
        monkeypatch.setattr(other, "send", lambda packet: accept(other, packet))
        service.command(f"{other.id}:2", "start", user=True)
    if scenario == "cancelled":
        service._cancel_call(service._meeting_id(peer, 2))
    packet = dict(captureId="legacy-manual", callGeneration=meeting["callGeneration"])
    if scenario == "wrongGeneration":
        packet["callGeneration"] = "unrelated-call"
    if scenario == "sole":
        assert service._native_start_guard(packet) is None
        with pytest.raises(StartCancelled):
            service._native_start_guard(packet)
    else:
        with pytest.raises(StartCancelled):
            service._native_start_guard(packet)


@pytest.mark.parametrize("automatic", [False, True])
def test_verified_native_socket_denies_missing_id_before_inputs_or_directory(server, automatic):  # noqa: F811
    import time

    from cassini_talk.browser_audio_proxy import BrowserAudioProxy
    from cassini_talk.recording_session import ProtocolError

    service, path, root = server
    peer = BrowserConnection(io.BytesIO(), browser_instance="verified-private-control")
    service.browsers[peer.id] = peer
    service.update(peer, dict(type="hello", version=1, capabilities=sorted(CAPABILITIES | {"independent-sessions-v1"})))
    service.update(peer, dict(type="configured", capabilities=sorted(CAPABILITIES)))
    service.update(
        peer,
        dict(
            type="meetings",
            meetings=[dict(POSITIVE["rtp"], title="Controlled call", url="https://fixture.invalid/call")],
        ),
    )
    service.update(peer, dict(type="status", recording=False))
    report(service, peer)
    service.auto_record = automatic

    def accept(packet):
        if packet.get("type") == "command":
            service.update(
                peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
            )

    peer.send = accept
    service.command(f"{peer.id}:2", "start", automatic=automatic, user=not automatic)
    client = BrowserAudioProxy(path=path)
    try:
        with pytest.raises(ProtocolError, match="requestId"):
            client.handle(
                dict(
                    type="start",
                    version=1,
                    independent=True,
                    captureId="unidentified-native",
                    timelineOriginMs=int(time.time() * 1000),
                    microphone=True,
                    callGeneration=POSITIVE["rtp"]["callGeneration"],
                )
            )
        assert not service.captures and not root.exists()
    finally:
        client.close()


def test_native_socket_rejects_same_id_replay_after_pre_directory_cancellation(server, monkeypatch):  # noqa: F811
    import queue
    import time

    from test_browser_capture import authorize_native_start

    from cassini_talk.browser_audio_proxy import BrowserAudioProxy
    from cassini_talk.recording_session import ProtocolError

    service, path, root = server
    ended = queue.Queue()
    audio = service._audio

    def completed(*args, **kwargs):
        try:
            return audio(*args, **kwargs)
        finally:
            ended.put(None)

    monkeypatch.setattr(service, "_audio", completed)
    token = authorize_native_start(service=service)
    packet = dict(
        type="start",
        version=1,
        independent=True,
        captureId="same-native",
        requestId=token,
        timelineOriginMs=-1,
        microphone=True,
    )
    for first in (True, False):
        client = BrowserAudioProxy(path=path)
        try:
            if not first:
                packet["timelineOriginMs"] = int(time.time() * 1000)
            with pytest.raises(ProtocolError, match="Invalid independent capture metadata" if first else "already"):
                client.handle(packet)
        finally:
            client.close()
        ended.get(timeout=5)
        assert not service.captures and not root.exists()
