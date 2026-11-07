# SPDX-License-Identifier: GPL-3.0-or-later
"""D74.1 actual broker/system writer proofs with synthetic devices and E1 barriers."""

import threading
from types import SimpleNamespace

import pytest
from durable import Changes, durable, saved
from test_browser_capture import server, stream  # noqa: F401
from test_call_signals import call

from cassini_talk.browser_service import connect, read_packet, request, send_packet
from cassini_talk.meeting_store import read_metadata
from cassini_talk.session_events import source_state


@pytest.fixture
def signals(server, monkeypatch):  # noqa: F811
    service, path, root = server
    changed, peers, commands = Changes(), [], []
    original_update = service.update
    original_start, original_stop = service._start_signal, service._stop_signal
    clock = [0.0]
    monkeypatch.setattr(service, "clock", lambda: clock[0])
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))

    def update(*args):
        try:
            return original_update(*args)
        finally:
            changed()

    def start(*args):
        try:
            return original_start(*args)
        finally:
            changed()

    def stop(*args):
        try:
            return original_stop(*args)
        finally:
            changed()

    monkeypatch.setattr(service, "update", update)
    monkeypatch.setattr(service, "_start_signal", start)
    monkeypatch.setattr(service, "_stop_signal", stop)

    def opened(instance="synthetic-profile", capabilities=("call-signals-v1",), profile=None):
        before = set(service.browsers)
        connection = connect(path)
        wire = connection.makefile("rwb")
        send_packet(wire, dict(role="extension", browserName="Synthetic browser", browserInstance=instance))
        send_packet(wire, dict(type="hello", version=1, capabilities=list(capabilities)))
        send_packet(wire, dict(type="status", recording=False, tabId=None))
        configure = read_packet(wire)
        assert configure == dict(type="configure", capabilities=["call-signals-v1"])
        send_packet(wire, dict(type="configured", capabilities=["call-signals-v1"]))
        changed.wait(lambda: any(b.id not in before and b.signals_configured for b in service.browsers.values()))
        owner = next(b for b in service.browsers.values() if b.id not in before)
        original_send = owner.send

        def sent(packet):
            if packet.get("type") == "command":
                commands.append(packet)
                changed()
            return original_send(packet)

        monkeypatch.setattr(owner, "send", sent)
        peer = SimpleNamespace(
            connection=connection, wire=wire, owner=owner, sequence=0, profile=instance if profile is None else profile
        )
        peers.append(peer)
        return peer

    def report(peer, calls, *, sequence=None):
        peer.sequence = peer.sequence + 1 if sequence is None else sequence
        send_packet(
            peer.wire, dict(type="callSignals", browserInstance=peer.profile, sequence=peer.sequence, tabs=calls)
        )
        changed.wait(lambda: peer.owner.signals_sequence >= peer.sequence)

    def active():
        assert not commands, "signal-only calls must never send a browser Start or Stop"
        identifier = service.signal_calls.capture_id
        capture = service.captures.get(identifier)
        return (
            capture
            if capture is not None
            and not (
                service.signal_calls.starting
                or service.signal_calls.retiring
                or capture.stop_requested.is_set()
                or capture.stopping
                or capture.finished
            )
            else None
        )

    def started():
        changed.wait(lambda: active() is not None, "system recording did not start")
        capture = active()
        for kind in tuple(capture.native_taps):
            stream(capture, kind)
        return capture

    def closed(peer):
        peer.wire.close()
        peer.connection.close()
        # _recover/recording callbacks are not involved in control disconnect.
        # Observe the broker's real disconnect transition explicitly.
        changed.wait(
            lambda: peer.owner.signals_instance not in service.signal_calls.snapshots,
            "signal peer did not withdraw its evidence",
        )

    # Disconnect is recorded in serve's finally, after update's last callback.
    original_disconnect = service.signal_calls.disconnect

    def disconnect(*args):
        original_disconnect(*args)
        changed()

    monkeypatch.setattr(service.signal_calls, "disconnect", disconnect)
    driver = SimpleNamespace(
        service=service,
        path=path,
        root=root,
        changed=changed,
        open=opened,
        report=report,
        started=started,
        closed=closed,
        commands=commands,
        clock=clock,
    )
    yield driver
    # Cancel/retire only fixtures owned by this service.
    for peer in peers:
        peer.wire.close()
        peer.connection.close()


def test_mixed_signal_records_real_system_others_and_positive_end_stops_with_audio(signals):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    capture = d.started()
    assert {t["kind"] for t in capture.host.session.manifest["tracks"]} == {"microphone", "system"}
    state = source_state(capture.host.session.manifest["events"])
    assert state.source == "system" and state.reason == "start" and not state.fallback
    assert read_metadata(capture.directory)["recordingDetails"]["othersRoute"] == dict(
        source="system", reason="mixedSiteSignal", site="Synthetic meeting"
    )
    assert not d.commands and not capture.host.source_outcomes
    d.report(p, [call(state="inactive")])
    saved(capture)
    assert capture.host.session.manifest["events"][-1]["reason"] == "callEnded"
    assert capture.host.session.manifest["state"] == "completed"


@pytest.mark.parametrize(
    "remote,state,opt_in",
    [
        ("mixed", "unknown", True),
        ("mixed", "active", False),
        ("separated", "active", True),
        ("unknown", "active", True),
    ],
)
def test_no_native_inputs_for_opt_out_passive_media_or_wrong_route(signals, remote, state, opt_in):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=opt_in)
    d.report(p, [call(remote=remote, state=state)])
    assert not d.service.captures and not d.commands


def test_shared_recording_refcounts_calls_and_preserves_fixed_system_route(signals):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call(), call(tab=4, generation="call-B")])
    capture = d.started()
    d.report(p, [call(remote="separated"), call(tab=4, generation="call-B")])
    assert len(d.service.captures) == 1 and not capture.stopping
    d.report(p, [call(state="inactive"), call(tab=4, generation="call-B")])
    assert len(d.service.signal_calls.calls) == 1 and not capture.stopping
    stream(capture, "system")
    d.report(p, [call(state="inactive"), call(tab=4, generation="call-B", state="inactive")])
    saved(capture)
    assert not d.commands


@pytest.mark.parametrize("action", ["stop-all", "stop"])
def test_manual_stop_suppresses_associated_generations_then_new_call_can_start(signals, monkeypatch, action):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call(), call(tab=4, generation="call-B")])
    capture = d.started()
    suppressed_at_stop = []
    original = capture.stop

    def stop(*args, **kwargs):
        suppressed_at_stop.append(set(d.service.signal_calls.suppressed))
        return original(*args, **kwargs)

    monkeypatch.setattr(capture, "stop", stop)
    request(action, path=d.path, **({"id": "capture:" + capture.capture_id} if action == "stop" else {}))
    expected = {(p.profile, 3, "call-A"), (p.profile, 4, "call-B")}
    assert suppressed_at_stop and expected <= suppressed_at_stop[0], "Stop must fence calls before input teardown"
    saved(capture)
    d.report(p, [call(), call(tab=4, generation="call-B")])
    assert len(d.service.captures) == 1
    d.report(p, [call(generation="call-C")])
    replacement = d.started()
    assert replacement is not capture and capture.finished


def test_replacement_peer_reconciles_same_call_and_ignores_old_end(signals):
    d = signals
    old = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(old, [call()])
    capture = d.started()
    new = d.open()
    d.report(new, [call()])
    d.report(old, [call(state="inactive")])
    assert not capture.stopping and d.service.signal_calls.capture_id == capture.capture_id
    assert len(d.service.captures) == 1
    d.report(new, [call(state="inactive")])
    saved(capture)


def test_control_loss_deadline_is_not_extended_by_native_audio(signals):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    capture = d.started()
    d.closed(p)
    for now in (40, 90, 119):
        d.clock[0] = now
        stream(capture, "system")
        stream(capture, "microphone")
        d.service.policy()
        assert not capture.stopping
    d.clock[0] = 120
    d.service.policy()
    saved(capture)
    assert capture.host.session.manifest["events"][-1]["reason"] == "pageGone"


@pytest.mark.parametrize("stop", ["end", "manual", "individual", "opt-out"])
def test_end_or_user_stop_during_held_start_cancels_before_opening_native_inputs(signals, monkeypatch, stop):
    d = signals
    entered, release, finished = (threading.Event() for _ in range(3))
    original = d.service._start_signal

    def held(*args):
        entered.set()
        durable(release, "test did not release signal startup")
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(d.service, "_start_signal", held)
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    try:
        d.report(p, [call()])
        durable(entered, "signal Start did not reach dispatch")
        capture = d.service.captures[d.service.signal_calls.capture_id]
        if stop == "end":
            d.report(p, [call(state="inactive")])
        elif stop == "opt-out":
            request("configure", path=d.path, autoRecord=False)
        elif stop == "individual":
            request("stop", path=d.path, id="capture:" + d.service.signal_calls.capture_id)
        else:
            request("stop-all", path=d.path)
        release.set()
        durable(finished, "canceled signal startup did not finish")
        assert not capture.native_taps and capture.directory is None
        assert not d.commands
    finally:
        release.set()


def test_known_mixed_rtp_receiver_is_not_a_browser_candidate_or_command(signals):
    from cassini_talk.browser_service import StartRefused

    d = signals
    p = d.open(capabilities=("call-signals-v1", "independent-sessions-v1", "call-evidence-v1"))
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    capture = d.started()
    send_packet(
        p.wire,
        dict(
            type="meetings",
            meetings=[
                dict(
                    tabId=3, title="Synthetic mixed call", url="https://meeting.example/test", sourceCount=1, heard=True
                )
            ],
        ),
    )
    d.changed.wait(lambda: 3 in p.owner.meetings)
    assert not request(path=d.path)["meetings"] and not d.commands
    with pytest.raises(StartRefused):
        d.service.command(d.service._meeting_id(p.owner, 3), "start", user=True)
    assert len(d.service.captures) == 1 and not capture.stopping and not d.commands


def test_duplicate_ack_and_stale_snapshot_cannot_resurrect_an_ended_call(signals):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()], sequence=10)
    capture = d.started()
    d.report(p, [call(state="inactive")], sequence=11)
    saved(capture)
    send_packet(p.wire, dict(type="configured", capabilities=["call-signals-v1"]))
    send_packet(p.wire, dict(type="callSignals", browserInstance=p.profile, sequence=10, tabs=[call()]))
    # A later fresh unknown snapshot is the FIFO processing barrier.
    d.report(p, [call(state="unknown")], sequence=12)
    assert len(d.service.captures) == 1 and p.owner.signals_sequence == 12


def test_new_generation_ends_old_recording_before_starting_the_next(signals):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    old = d.started()
    d.report(p, [call(generation="call-B")])
    new = d.started()
    assert old.finished and old is not new
    assert old.host.session.manifest["events"][-1]["reason"] == "callEnded"


def test_application_readiness_gates_system_start(signals):
    d = signals
    d.service.require_application = True
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    assert not d.service.captures
    lease = connect(d.path)
    try:
        with lease.makefile("rwb") as wire:
            send_packet(wire, dict(role="application", active=True, generation=1))
            assert read_packet(wire) == dict(ready=True, protocol=2)
        capture = d.started()
        assert {kind for kind in capture.native_taps} == {"microphone", "system"}
    finally:
        lease.close()


def test_shared_recording_keeps_the_users_microphone_choice(signals):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    capture = d.started()
    capture.select(False, True)
    d.report(p, [call(), call(tab=4, generation="call-B")])
    assert not capture.me and capture.others and "microphone" not in capture.native_taps
    assert len(d.service.captures) == 1


def test_reconnect_during_pending_start_keeps_the_same_reservation(signals, monkeypatch):
    d = signals
    entered, release, finished = (threading.Event() for _ in range(3))
    original = d.service._start_signal

    def held(*args):
        entered.set()
        durable(release, "test did not release signal Start")
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(d.service, "_start_signal", held)
    old = d.open()
    request("configure", path=d.path, autoRecord=True)
    try:
        d.report(old, [call()])
        durable(entered, "signal Start did not reach its reservation")
        identifier = d.service.signal_calls.capture_id
        d.closed(old)
        replacement = d.open()
        d.report(replacement, [call()])
        assert d.service.signal_calls.capture_id == identifier and len(d.service.captures) == 1
        release.set()
        durable(finished, "reconciled Start did not finish")
        capture = d.started()
        assert capture.capture_id == identifier and len(d.service.captures) == 1
    finally:
        release.set()


def test_unknown_at_input_admission_waits_without_opening_and_end_cancels(signals, monkeypatch):
    d = signals
    entered, release, admitting, finished = (threading.Event() for _ in range(4))
    original_start = d.service._start_signal
    original_admit = d.service._admit_signal

    def held(*args):
        entered.set()
        durable(release, "test did not release dispatch")
        try:
            return original_start(*args)
        finally:
            finished.set()

    def admit(*args):
        admitting.set()
        return original_admit(*args)

    monkeypatch.setattr(d.service, "_start_signal", held)
    monkeypatch.setattr(d.service, "_admit_signal", admit)
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    try:
        d.report(p, [call()])
        durable(entered, "signal Start did not reach dispatch")
        capture = d.service.captures[d.service.signal_calls.capture_id]
        d.report(p, [call(state="unknown")])
        release.set()
        durable(admitting, "Start did not revalidate before native input admission")
        assert not capture.native_taps
        d.report(p, [call(state="inactive")])
        durable(finished, "ended pending call did not cancel input admission")
        assert not capture.native_taps and capture.discarded
    finally:
        release.set()


def test_positive_end_never_stops_an_unrelated_manual_capture(signals):
    from test_browser_capture import remote, start

    from cassini_talk.browser_audio_proxy import BrowserAudioProxy

    d = signals
    manual = BrowserAudioProxy(path=d.path)
    try:
        start(manual, "unrelated-manual")
        remote(manual, "voice")
        p = d.open()
        request("configure", path=d.path, autoRecord=True)
        d.report(p, [call()])
        automatic = d.started()
        d.report(p, [call(state="inactive")])
        saved(automatic)
        unrelated = d.service.captures["unrelated-manual"]
        assert not unrelated.finished and not unrelated.stop_requested.is_set()
    finally:
        manual.handle(dict(type="stop", captureId="unrelated-manual"))
        manual.close()


def test_no_stable_profile_identity_cannot_create_signal_ownership(signals):
    d = signals
    p = d.open(instance=None)
    request("configure", path=d.path, autoRecord=True)
    send_packet(p.wire, dict(type="callSignals", sequence=1, tabs=[call()]))
    assert read_packet(p.wire) is None  # Actual control EOF is the E1 rejection event.
    assert not d.service.captures and not d.service.signal_calls.calls


def test_stable_signal_profile_can_own_calls_without_native_parent_pid(signals):
    d = signals
    p = d.open(instance=None, profile="opaque-persistent-profile")
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    capture = d.started()
    assert next(iter(d.service.signal_calls.calls))[0] == "opaque-persistent-profile"
    assert capture.signal_owned


def test_evidence_lost_during_route_checkpoint_is_revalidated_before_inputs(signals, monkeypatch):
    from cassini_talk import meeting_store

    d = signals
    entered, release, revalidated, finished = (threading.Event() for _ in range(4))
    write = meeting_store.update_metadata
    original_admit, original_start = d.service._admit_signal, d.service._start_signal
    admissions = []

    def checkpoint(*args, **kwargs):
        if kwargs.get("recordingDetails", {}).get("othersRoute"):
            entered.set()
            durable(release, "test did not release route checkpoint")
        return write(*args, **kwargs)

    def admit(*args):
        admissions.append(True)
        if len(admissions) == 2:
            revalidated.set()
        return original_admit(*args)

    def start(*args):
        try:
            return original_start(*args)
        finally:
            finished.set()

    monkeypatch.setattr(meeting_store, "update_metadata", checkpoint)
    monkeypatch.setattr(d.service, "_admit_signal", admit)
    monkeypatch.setattr(d.service, "_start_signal", start)
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    try:
        d.report(p, [call()])
        durable(entered, "route checkpoint did not begin")
        capture = d.service.captures[d.service.signal_calls.capture_id]
        d.report(p, [call(state="unknown")])
        release.set()
        durable(revalidated, "eligibility was not checked after the held checkpoint")
        assert not capture.native_taps
        d.report(p, [call(state="inactive")])
        durable(finished, "ended Start did not cancel")
        assert capture.directory is None and not capture.native_taps and not d.commands
    finally:
        release.set()


@pytest.mark.parametrize(
    "ended",
    [call(generation="call-B", state="inactive"), call(document="document-reloaded", state="inactive")],
    ids=["coalesced-other-call-end", "reloaded-document-end"],
)
def test_different_generation_end_starts_bounded_loss_without_extending_it(signals, ended):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    d.report(p, [call()])
    capture = d.started()
    d.clock[0] = 10
    d.report(p, [ended])
    association = next(iter(d.service.signal_calls.calls.values()))
    assert association.lost_at == 10, "A replacement snapshot must withdraw the absent association's evidence"
    assert not capture.stop_requested.is_set(), "A different generation's end is not this call's positive end"
    for now in (40, 90, 129):
        d.clock[0] = now
        d.report(p, [ended])
        stream(capture, "system")
        assert association.lost_at == 10 and not capture.stop_requested.is_set()
    d.clock[0] = 130
    d.service.policy()
    saved(capture)
    assert capture.host.session.manifest["state"] == "completed"
    assert capture.host.session.manifest["events"][-1]["reason"] == "pageGone"
    assert not d.commands


@pytest.mark.parametrize("stage", ["no-owner", "recording", "retiring"])
def test_stop_all_suppresses_current_mixed_snapshot_before_policy_admission(signals, monkeypatch, stage):
    d = signals
    p = d.open()
    request("configure", path=d.path, autoRecord=True)
    capture = None
    if stage != "no-owner":
        d.report(p, [call()])
        capture = d.started()
    # update() commits the full snapshot before calling policy(). Hold only
    # policy dispatch, so Stop All orders between those real broker operations.
    blocked, release = threading.Event(), threading.Event()
    original = d.service.policy

    def deferred():
        if release.is_set():
            return original()
        blocked.set()

    monkeypatch.setattr(d.service, "policy", deferred)
    try:
        first = [] if stage == "no-owner" else [call(state="inactive" if stage == "retiring" else "active")]
        d.report(p, [*first, call(tab=4, generation="call-B")])
        durable(blocked, "updated snapshot did not reach policy dispatch")
        assert all(key[1] != 4 for key in d.service.signal_calls.calls), "B must still be waiting for admission"
        if stage == "retiring":
            assert d.service.signal_calls.retiring == "callEnded"
        before = len(d.service.captures)
        request("stop-all", path=d.path)
        if capture is not None:
            saved(capture)
        release.set()
        d.service.policy()
        assert len(d.service.captures) == before, "The user's Stop must fence the not-yet-admitted current call"
        assert not d.service.signal_calls.calls
        d.closed(p)
        replacement = d.open()
        d.report(replacement, [call(tab=4, generation="call-B")])
        assert len(d.service.captures) == before, "Pending-call Stop suppression must survive a control reconnect"
        d.report(replacement, [call(tab=4, generation="call-C")])
        next_capture = d.started()
        assert next_capture is not capture and len(d.service.captures) == before + 1
    finally:
        release.set()
