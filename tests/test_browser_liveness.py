# SPDX-License-Identifier: GPL-3.0-or-later
"""G7: a frozen companion keeps its sockets open; deadlines on an injected clock close them."""

import json
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from broker_fixture import stop_broker
from durable import DURABLE_BOUND, durable, saved
from test_browser_capture import Tap, authorize_native_start, local
from test_browser_capture import start as start_audio
from test_browser_service import wait_for
from test_browser_sessions import Extension
from test_capture_record import declare, pcm

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.browser_service import PCM_STALE_AFTER, WRITE_DEADLINE, BrowserService, send_packet
from cassini_talk.recording_session import ProtocolError

ACK = dict(type="ack")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def payload_identity():
    # This suite never edits the application payload. Hash its real bytes once;
    # dedicated broker-identity tests still exercise live updates and mismatches.
    from cassini_talk.broker_identity import expected

    return expected()


@pytest.fixture
def broker(tmp_path, monkeypatch, payload_identity):
    from cassini_talk import broker_identity

    expected = broker_identity.expected
    monkeypatch.setattr(
        broker_identity, "expected", lambda root=None: expected(root) if root is not None else dict(payload_identity)
    )
    Tap.instances = []
    clock = Clock()
    service = BrowserService(
        tmp_path / "config.json",
        capture_parent=tmp_path / "library",
        tap_factory=Tap,
        notifier=lambda _title, _stop: None,
        clock=clock,
    )
    # Fallback is observed as it commits, never by polling the manifest.
    lost = threading.Event()
    changed = service._capture_changed

    def observe(capture, event):
        changed(capture, event)
        if event == "sources" and capture.browser_lost:
            lost.set()

    monkeypatch.setattr(service, "_capture_changed", observe)
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    yield service, path, clock, lost
    stop_broker(service, worker, path)


class Settled:
    """E1: wait for the broker to finish each frame before the test moves on.

    handle() returns once the reply is read, but the broker thread clears
    write_started, then stamps last_inbound and clears handling, just after.
    Advancing the fake clock and ticking first would blame a healthy peer.
    """

    def __init__(self, client, service):
        self.client, self.service = client, service

    def handle(self, packet):
        reply = self.client.handle(packet)
        wait_for(self.idle)
        return reply

    def idle(self):
        with self.service.lock:
            transports = list(self.service._audio_transports)
        return all(t.shut_down.is_set() or t.write_started is None and not t.handling for t in transports)

    def __getattr__(self, name):
        return getattr(self.client, name)


def negotiated(client, service, identifier):
    reply = client.handle(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId=identifier,
            requestId=authorize_native_start(service=service),
            timelineOriginMs=int(time.time() * 1000),
            microphone=True,
            title=identifier,
            features=["source-outcomes-v1"],
        )
    )
    assert reply["features"] == ["source-outcomes-v1"]
    capture = service.captures[identifier]
    # A control peer advertising multi-stream-v1 sets this from policy().
    capture.multistream_capable = True
    return capture


def lost_events(capture):
    manifest = json.loads((capture.directory / "session.json").read_text())
    return [e for e in manifest["events"] if e["type"] == "othersSource" and e.get("reason") == "lost"]


def jam(browser):
    """Leave the broker's writer genuinely blocked on a peer that never reads."""
    browser.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

    def flood():
        for _ in range(4):
            browser.send(dict(type="flood", pad="x" * 400_000))

    # A synchronous send() would block here forever; fail fast instead.
    sender = threading.Thread(target=flood, daemon=True)
    sender.start()
    sender.join(2)
    assert not sender.is_alive(), "send() blocked on a peer that never reads"
    # The first 400 KB write cannot fit a 4 KiB buffer, so it stays blocked.
    wait_for(lambda: browser.write_started is not None)


def test_the_serve_loop_enforces_deadlines_without_an_explicit_tick(broker):
    service, path, clock, _lost = broker
    peer = Extension(path, (3,))
    (browser,) = service.browsers.values()
    try:
        jam(browser)
        clock.now = browser.write_started + WRITE_DEADLINE
        # No tick() here: the serve loop's periodic pass must close the peer.
        assert browser.shut_down.wait(5)
        wait_for(lambda: browser.id not in service.browsers)
    finally:
        peer.close()


def test_write_deadline_closes_a_peer_that_never_reads(broker):
    service, path, clock, _lost = broker
    peer = Extension(path, (3,))
    (browser,) = service.browsers.values()
    try:
        jam(browser)
        started = browser.write_started
        clock.now = started + WRITE_DEADLINE - 0.5
        service.tick()
        assert not browser.shut_down.is_set(), "A slow write is not yet a stuck one"
        clock.now = started + WRITE_DEADLINE
        service.tick()
        assert browser.shut_down.wait(5)
        browser.writer.join(5)
        assert not browser.writer.is_alive()
        # Commands never waited behind the stuck write, and none queue after it.
        assert browser.order_lock.acquire(blocking=False)
        browser.order_lock.release()
        with pytest.raises(OSError):
            browser.send(dict(type="command", action="stop", tabId=3))
        # The reader unwinds through the existing EOF path and forgets the peer.
        wait_for(lambda: browser.id not in service.browsers)
        assert browser.closed
    finally:
        peer.close()


def test_outbound_overflow_closes_the_peer_instead_of_growing(broker):
    service, path, _clock, _lost = broker
    peer = Extension(path, (3,))
    (browser,) = service.browsers.values()
    try:
        jam(browser)
        with pytest.raises(OSError):
            for _ in range(100):
                browser.send(dict(type="command", action="stop", tabId=3))
        assert browser.shut_down.is_set() and not browser.outbound
        wait_for(lambda: browser.id not in service.browsers)
    finally:
        peer.close()


def test_stop_finishes_and_policy_resumes_under_a_stuck_writer(broker, monkeypatch):
    service, path, _clock, _lost = broker
    deadline_clock = Clock()
    service.deadline_clock = deadline_clock
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    worker = ThreadPoolExecutor(1)
    expire = threading.Event()
    try:
        start_audio(client, "stuck-stop", service=service)
        local(client, "me")
        capture = service.captures["stuck-stop"]
        state = dict(recording=True, tabId=3, captureId="stuck-stop", path=str(capture.directory))
        send_packet(peer.stream, dict(type="status", recording=True, sessions=[state]))
        (browser,) = service.browsers.values()
        wait_for(lambda: browser.states.get(3, {}).get("captureId") == "stuck-stop")
        jam(browser)

        generation = service._stop_generation
        resumed, stop_queued = threading.Event(), threading.Event()
        send = browser.send
        policy = service.policy

        def observed():
            if service._stop_generation > generation and not service._stopping_all.is_set():
                resumed.set()
            policy()

        def command_wait(_timeout):
            durable(expire, "the Stop command did not receive its virtual deadline")
            return False  # The jammed connection has never answered.

        def observe_send(packet):
            if packet.get("action") == "stop":
                pending = browser.pending[packet["requestId"]]
                monkeypatch.setattr(pending["event"], "wait", command_wait)
                send(packet)
                stop_queued.set()
            else:
                send(packet)

        monkeypatch.setattr(service, "policy", observed)
        monkeypatch.setattr(browser, "send", observe_send)
        stopping = worker.submit(service.stop_all)
        durable(stop_queued, "Stop was not queued on the jammed browser")
        saved(capture)
        deadline_clock.now = service.batch_timeout
        expire.set()
        result = stopping.result(timeout=DURABLE_BOUND)
        assert str(capture.directory) in result["stoppedPaths"]
        manifest = json.loads((capture.directory / "session.json").read_text())
        assert manifest["state"] == "completed", manifest.get("problem")
        # A future stuck in a socket write used to hold _stopping_all forever,
        # pausing every fallback and automatic start behind one frozen peer.
        durable(resumed, "broker policy remained paused after the Stop batch")
        assert browser.write_started is not None, "The peer is still not reading"
    finally:
        expire.set()
        client.close()
        peer.close()
        worker.shutdown(wait=False, cancel_futures=True)


def test_frozen_audio_transport_falls_back_to_system_without_opening_native_me(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "frozen")
        assert capture.microphone is None
        assert client.handle(dict(declare("me", "sender"), kind="local", label="Microphone")) == ACK
        assert client.handle(pcm("me", 480, value=b"\x01\x00")) == ACK
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        # Freeze: the companion neither sends nor reads; its socket stays open.
        clock.now = PCM_STALE_AFTER - 0.5
        service.tick()
        assert not lost.is_set() and not capture.browser_lost
        clock.now = PCM_STALE_AFTER
        service.tick()
        durable(lost, "the fallback did not commit")
        (event,) = lost_events(capture)
        assert event["source"] == "system"
        assert capture.native_taps.keys() == {"system"} and capture.microphone is None
        system_id, system_tap = capture.native_taps["system"]
        system_track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == system_id)
        before = system_track["frames"]
        system_tap.append(b"\x02\x00" * 480, system_track["startOffsetMs"] + 10)
        assert system_track["frames"] > before
        assert not capture.finished
        service.command("capture:frozen", "stop")
        manifest = json.loads((capture.directory / "session.json").read_text())
        # Saved; the frozen browser spans never sent trackEnd, and Me never opened a raw OS mic.
        assert manifest["state"] == "interrupted" and manifest["problemKind"] == "capture"
        assert [t["kind"] for t in manifest["tracks"]] == ["local", "remote", "system"]
        assert manifest["tracks"][-1]["startOffsetMs"] == event["atMs"]
    finally:
        client.close()


def test_stuck_audio_reply_is_bounded_by_the_write_deadline(broker, caplog):
    service, path, clock, lost = broker
    client = BrowserAudioProxy(path=path)
    try:
        capture = negotiated(client, service, "unread-replies")
        # Disarm staleness, so only the write deadline can close this transport.
        capture.multistream_capable = False
        assert client.handle(declare("guest", "receiver")) == ACK
        (transport,) = service._audio_transports
        transport.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        # Keep sending and never read an acknowledgement, until replies block.
        sender = threading.Thread(target=flood_audio, args=(client, transport), daemon=True)
        sender.start()

        def overdue():
            # A reply finishing between steps is restamped at the later time;
            # the one blocked on the full buffer stays overdue and is closed.
            clock.now += WRITE_DEADLINE
            service.tick()
            return transport.shut_down.is_set()

        with caplog.at_level(logging.WARNING, logger="cassini_talk.browser_service"):
            wait_for(overdue)
        assert "Closing a browser peer that stopped reading" in caplog.text
        assert "stopped sending audio" not in caplog.text
        durable(lost, "the fallback did not commit")
        assert lost_events(capture)
        sender.join(5)
        assert not sender.is_alive()
    finally:
        client.close()


def flood_audio(client, transport):
    sequence = 0
    try:
        while not transport.shut_down.is_set():
            # Unbuffered, so closing the client later has nothing left to flush.
            packet = pcm("guest", 48, sequence=sequence, offset=sequence)
            client.connection.sendall(json.dumps(packet).encode() + b"\n")
            sequence += 1
    except OSError:
        pass  # The broker shut this transport down.


def test_no_fallback_before_the_first_remote_pcm(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "pre-join")
        assert client.handle(declare("guest", "receiver")) == ACK
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        # A round trip after the tick proves the transport is still open.
        assert client.handle(pcm("guest", 480)) == ACK
        assert not capture.browser_lost and not lost.is_set()
        assert not lost_events(capture)
        capture.stop()
    finally:
        client.close()


def test_no_fallback_while_pcm_keeps_arriving(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "healthy")
        assert client.handle(declare("guest", "receiver")) == ACK
        for sequence in range(8):
            assert client.handle(pcm("guest", 480, sequence=sequence, offset=sequence * 10)) == ACK
            clock.now += PCM_STALE_AFTER - 0.5
            service.tick()
        assert clock.now > 4 * PCM_STALE_AFTER
        assert client.handle(pcm("guest", 480, sequence=8, offset=80)) == ACK
        assert not capture.browser_lost and not lost.is_set()
        assert not lost_events(capture)
        capture.stop()
    finally:
        client.close()


def end(identifier):
    return dict(type="trackEnd", id=identifier, reason="ended", droppedFrames=0, endOffsetMs=10)


def test_an_ended_remote_span_leaves_its_quiet_transport_open(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "guest-left")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        # The guest left, or the receiver dropped out during a network blip:
        # the extension ends the span and correctly stops sending.
        assert client.handle(end("guest")) == ACK
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set()
        assert client.handle(declare("guest-back", "receiver")) == ACK
        assert not capture.browser_lost and not lost.is_set() and not lost_events(capture)
        capture.stop()
    finally:
        client.close()


def test_a_rejoined_span_is_recorded_and_watched_again(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "rejoin")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        assert client.handle(end("guest")) == ACK
        clock.now = PCM_STALE_AFTER + 5
        service.tick()
        # The guest rejoins: a new span on the same transport, recorded.
        assert client.handle(declare("guest-again", "receiver")) == ACK
        assert client.handle(pcm("guest-again", 480, offset=PCM_STALE_AFTER * 1000)) == ACK
        tracks = {t["id"]: t for t in capture.host.session.manifest["tracks"]}
        assert tracks["guest-again"]["frames"] == 480
        (transport,) = service._audio_transports
        clock.now += PCM_STALE_AFTER - 0.5
        service.tick()
        assert not transport.shut_down.is_set() and not lost_events(capture)
        # The new open span re-arms the watchdog: freezing now still falls back.
        clock.now += 0.5
        service.tick()
        durable(lost, "the fallback did not commit")
        assert len(lost_events(capture)) == 1
        capture.stop()
    finally:
        client.close()


def test_a_recorder_waiting_out_its_longest_retry_is_not_cut_over(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "retrying")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        # capture.js retryDelay() waits up to 30 s before reattaching a failed
        # recorder, sending nothing meanwhile, not even trackEnd; its first
        # new packet may then take up to the 10 s acknowledgement limit.
        clock.now = 30 + 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set()
        assert client.handle(end("guest")) == ACK
        assert client.handle(declare("guest-retry", "receiver")) == ACK
        assert not capture.browser_lost and not lost.is_set()
        capture.stop()
    finally:
        client.close()


def test_a_navigated_frames_dead_spans_do_not_keep_the_watchdog_armed(broker):
    service, path, clock, _lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "reloaded")
        assert client.handle(declare("old-frame", "receiver")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        # The page reloads: the old document's recorder dies without trackEnd.
        # navigating retires its span; policy falls back, and the new
        # document's span restores the browser. A fallback alone no longer
        # disarms a span (D57): only its end or its document's retirement.
        send_packet(
            peer.stream,
            dict(
                type="status",
                recording=True,
                sessions=[dict(recording=True, tabId=3, captureId="reloaded", pageState="navigating")],
            ),
        )
        wait_for(lambda: capture.streams.tracks["old-frame"].dormant)
        capture.fallback()
        send_packet(
            peer.stream,
            dict(
                type="status",
                recording=True,
                sessions=[dict(recording=True, tabId=3, captureId="reloaded", pageState="present")],
            ),
        )
        peer.inventory((3,))
        wait_for(lambda: not capture.streams.departing)
        assert client.handle(declare("new-frame", "receiver-reloaded")) == ACK
        assert client.handle(pcm("new-frame", 480, offset=1000)) == ACK
        capture.restore()
        assert not capture.browser_lost
        # Later the guest leaves: the only live span ends and the transport is quiet.
        assert client.handle(end("new-frame")) == ACK
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set(), "a dead frame's span is not a running recorder"
        assert client.handle(declare("guest-back", "receiver-back")) == ACK
        assert not capture.browser_lost and len(lost_events(capture)) == 1, "only the reload's own fallback"
        capture.stop()
    finally:
        client.close()
        peer.close()


def test_the_brokers_own_stall_is_not_blamed_on_the_peer(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "slow-disk")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        (transport,) = service._audio_transports
        handle = capture.handle
        during = []

        def stalled(message):
            # A long write under the capture lock: fake time passes and the
            # serve loop ticks while the peer waits for this acknowledgement.
            clock.now += PCM_STALE_AFTER * 2
            service.tick()
            during.append(transport.shut_down.is_set())
            return handle(message)

        capture.handle = stalled
        assert client.handle(pcm("guest", 480, sequence=1, offset=10)) == ACK
        capture.handle = handle
        assert during == [False]
        clock.now += PCM_STALE_AFTER - 1
        service.tick()
        assert not transport.shut_down.is_set(), "the wait restarts once the peer could send again"
        assert client.handle(pcm("guest", 480, sequence=2, offset=20)) == ACK
        assert not capture.browser_lost and not lost.is_set()
        capture.stop()
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["sources", "track", "trackEnd"])
def test_every_frame_on_the_audio_transport_counts(broker, kind):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        negotiated(client, service, "frames-" + kind)
        assert client.handle(declare("guest", "receiver-a")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        assert client.handle(declare("second", "receiver-b")) == ACK
        assert client.handle(pcm("second", 480)) == ACK
        clock.now = 30
        frame = dict(
            sources=dict(type="sources", discoveredSources=[dict(sourceTrackId="receiver-a", kind="remote")]),
            track=declare("third", "receiver-c"),
            trackEnd=end("second"),  # "guest" stays open, so the watchdog stays armed.
        )[kind]
        assert client.handle(frame) == ACK
        (transport,) = service._audio_transports
        clock.now = 30 + PCM_STALE_AFTER - 0.5
        service.tick()
        assert not transport.shut_down.is_set(), f"a {kind} frame did not restart the wait"
        clock.now = 30 + PCM_STALE_AFTER
        service.tick()
        assert transport.shut_down.is_set()
        durable(lost, "the fallback did not commit")
    finally:
        client.close()


def test_a_capture_without_multistream_control_is_never_cut_over(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "legacy-control")
        capture.multistream_capable = False
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set()
        assert client.handle(pcm("guest", 480, sequence=1, offset=10)) == ACK
        assert not capture.browser_lost and not lost.is_set()
        capture.stop()
    finally:
        client.close()


@pytest.mark.parametrize("ending", ["requested", "finished"])
def test_a_stopping_capture_is_never_cut_over(broker, ending):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "stopping")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        if ending == "requested":
            # Taps may take a while to finalize; a quiet transport is expected.
            capture.stop_requested.set()
        else:
            capture.stop()
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set()
        assert not capture.browser_lost and not lost.is_set() and not lost_events(capture)
        capture.stop()
    finally:
        client.close()


def test_unknown_extension_message_types_are_ignored(broker):
    service, path, _clock, _lost = broker
    peer = Extension(path, (3,))
    (browser,) = service.browsers.values()
    try:
        send_packet(peer.stream, dict(type="pong", seq=1))
        send_packet(peer.stream, dict(type="from-a-newer-companion", detail={"nested": True}))
        send_packet(peer.stream, dict(type=["not", "a", "name"]))
        peer.inventory((3, 4))
        wait_for(lambda: len(service.snapshot()["meetings"]) == 2)
        assert service.browsers.get(browser.id) is browser and not browser.closed
        assert browser.ignored == {"from-a-newer-companion", "list"}
    finally:
        peer.close()


def test_pcm_stale_after_is_pinned_by_d52_5():
    # Above the extension's longest retry wait plus its acknowledgement limit
    # (30 + 10 s) and below the 60 s span cut; D52.5 sets 45 s.
    assert 30 + 10 < PCM_STALE_AFTER < 60
    assert PCM_STALE_AFTER == 45


def test_an_open_span_quiet_from_zero_is_closed_at_exactly_45_seconds(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "boundary")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        (transport,) = service._audio_transports
        clock.now = 44.999
        service.tick()
        assert not transport.shut_down.is_set()
        clock.now = 45
        service.tick()
        assert transport.shut_down.is_set()
        durable(lost, "the fallback did not commit")
        assert lost_events(capture)
    finally:
        client.close()


STALE_VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "browser-protocol/conformance/multistream/stale-watchdog.json").read_text()
)


def test_stale_watchdog_constant_matches_the_shared_vectors():
    assert STALE_VECTORS["constants"] == dict(PCM_STALE_AFTER=PCM_STALE_AFTER * 1000)


@pytest.mark.parametrize("case", STALE_VECTORS["cases"], ids=lambda case: case["name"])
def test_shared_stale_watchdog_vectors_on_the_real_transport(broker, case):
    replay(broker, case)


# Shapes the shared vectors do not have yet, replayed by the same runner.
LOCAL_STALE_CASES = [
    # Statuses at one time: the last must be applied before the span after
    # it is declared, or b lands in the departing window and never arms.
    dict(
        name="statuses-at-one-time-apply-in-order",
        frames=[
            dict(atMs=0, frame="track", id="a"),
            dict(atMs=0, frame="audio", id="a"),
            *(
                dict(
                    atMs=1000,
                    frame="documentChanged",
                    pageState=page,
                    documentGeneration=f"doc-{n + (page == 'present')}",
                )
                for n in range(1, 5)
                for page in ("navigating", "present")
            ),
            dict(atMs=1000, frame="track", id="b"),
            dict(atMs=1000, frame="audio", id="b"),
        ],
        closeAtMs=46000,
        probes=[dict(atMs=45999, closed=False), dict(atMs=46000, closed=True)],
    ),
]


@pytest.mark.parametrize("case", LOCAL_STALE_CASES, ids=lambda case: case["name"])
def test_local_stale_watchdog_cases_on_the_real_transport(broker, case):
    replay(broker, case)


def replay(broker, case):
    """Replay audio-transport and control frames; tick at every frame, probe and deadline time."""
    service, path, clock, _lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        identifier = "stale-" + case["name"][:40]
        negotiated(client, service, identifier)
        (transport,) = service._audio_transports
        (control,) = service.browsers.values()
        sequences, ended = {}, set()
        times = {f["atMs"] for f in case["frames"]} | {p["atMs"] for p in case["probes"]}
        times |= {f["atMs"] + PCM_STALE_AFTER * 1000 for f in case["frames"]}
        closed_at = None
        for at in sorted(times):
            clock.now = at / 1000
            # A frame arriving exactly at the deadline is too late: tick first.
            service.tick()
            if closed_at is None and transport.shut_down.is_set():
                closed_at = at
            for frame in (f for f in case["frames"] if f["atMs"] == at and closed_at is None):
                kind = frame["frame"]
                if kind == "control":
                    send_packet(peer.stream, dict(type="pong", seq=0))
                    wait_for(lambda: control.last_inbound == clock.now)
                    continue
                if kind == "documentChanged":
                    # D57: the recording tab's page state and document, as the
                    # extension's status reports them on the control link.
                    entry = dict(recording=True, tabId=3, captureId=identifier)
                    for key in ("pageState", "documentGeneration"):  # either may be absent
                        if key in frame:
                            entry[key] = frame[key]
                    # Each status and inventory replaces its dict: wait for that,
                    # since last_inbound already matches when two share a time.
                    pages = control.status_pages
                    send_packet(peer.stream, dict(type="status", recording=True, sessions=[entry]))
                    wait_for(lambda before=pages: control.status_pages is not before)
                    with service.lock:
                        pass  # update() holds the lock until the status is applied.
                    # navigating zeroes the row's count; the page keeps its call.
                    meetings = control.meetings
                    peer.inventory((3,), page=frame.get("pageState"), document=frame.get("documentGeneration"))
                    wait_for(lambda before=meetings: control.meetings is not before)
                    with service.lock:
                        pass
                    continue
                if kind == "track":
                    packet = declare(frame["id"], "receiver-" + frame["id"])
                    if "documentGeneration" in frame:  # D61: the extension's stamp
                        packet["documentGeneration"] = frame["documentGeneration"]
                elif kind == "audio":
                    sequence = sequences.get(frame["id"], 0)
                    sequences[frame["id"]] = sequence + 1
                    packet = pcm(frame["id"], 480, sequence=sequence, offset=at + 1)
                elif kind == "sources":
                    packet = dict(type="sources", discoveredSources=[dict(sourceTrackId="receiver-a", kind="remote")])
                elif kind == "trackEnd":
                    packet = end(frame["id"])
                else:
                    packet = dict(type="stop", captureId=identifier)
                reply = client.handle(packet)
                if kind in ("track", "audio") and frame["id"] in ended:
                    # An ended id stays ended: its repeat and audio are refused.
                    assert (reply.get("type"), reply.get("code")) == ("error", "state"), (frame, reply)
                else:
                    assert reply.get("type") in ("ack", "stopped"), (frame, reply)
                if kind == "trackEnd":
                    ended.add(frame["id"])
            for probe in (p for p in case["probes"] if p["atMs"] == at):
                assert (closed_at is not None) is probe["closed"], (case["name"], probe)
        assert closed_at == case["closeAtMs"], case["name"]
    finally:
        client.close()
        peer.close()


@pytest.mark.parametrize("late", [False, True], ids=["before-the-deadline", "after-the-deadline"])
def test_a_frame_after_the_deadline_is_too_late_before_any_tick(broker, late):
    """The shared runner ticks first; here the late frame arrives between ticks."""
    service, path, clock, _lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        negotiated(client, service, "stale-between-ticks")
        (transport,) = service._audio_transports
        clock.now = 0
        assert client.handle(declare("a", "receiver-a")).get("type") == "ack"
        assert client.handle(pcm("a", 480, sequence=0, offset=1)).get("type") == "ack"
        clock.now = PCM_STALE_AFTER + (0.3 if late else -0.3)
        try:
            reply = client.handle(pcm("a", 480, sequence=1, offset=2))
        except (ConnectionError, OSError, ProtocolError):
            reply = None
        if late:
            assert reply is None or reply.get("type") != "ack"
            assert transport.shut_down.wait(5)
        else:
            assert reply.get("type") == "ack"
            clock.now = PCM_STALE_AFTER + 0.3
            service.tick()
            assert not transport.shut_down.is_set()
    finally:
        client.close()


def report(peer, capture, page, document=None):
    """The extension's status for the recording's tab (background.js controlStatus)."""
    entry = dict(recording=True, tabId=3, captureId=capture.capture_id, pageState=page)
    if document is not None:
        entry["documentGeneration"] = document
    send_packet(peer.stream, dict(type="status", recording=True, sessions=[entry]))


def seen_document(capture, document):
    wait_for(lambda: capture.document_generation == document)


def test_a_fast_reload_retires_the_dead_documents_spans_without_falling_back(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "fast-reload")
        assert client.handle(declare("old-frame", "doc-1:source:1")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        report(peer, capture, "present", "doc-1")
        seen_document(capture, "doc-1")
        # pagehide: keepOnNavigation keeps the session; the old recorder dies
        # without trackEnd, and the page returns before any fallback.
        report(peer, capture, "navigating", "doc-1")
        wait_for(lambda: capture.streams.tracks["old-frame"].dormant)
        report(peer, capture, "present", "doc-2")
        peer.inventory((3,))
        seen_document(capture, "doc-2")
        assert client.handle(declare("new-frame", "doc-2:source:1")) == ACK
        assert client.handle(pcm("new-frame", 480, offset=1000)) == ACK
        # Later the guest leaves: the only live span ends, a healthy quiet.
        assert client.handle(end("new-frame")) == ACK
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set(), "a gone document's span is not a running recorder"
        assert client.handle(declare("guest-back", "doc-2:source:2")) == ACK
        assert not capture.browser_lost and not lost.is_set() and not lost_events(capture)
        capture.stop()
    finally:
        client.close()
        peer.close()


def test_a_missed_navigation_is_caught_by_a_new_document_generation(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "missed-navigation")
        assert client.handle(declare("old-frame", "doc-1:source:1")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        report(peer, capture, "present", "doc-1")
        seen_document(capture, "doc-1")
        # The navigating status never arrived (control was down meanwhile).
        report(peer, capture, "present", "doc-2")
        wait_for(lambda: capture.streams.tracks["old-frame"].dormant)
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set()
        assert client.handle(declare("guest", "doc-2:source:1")) == ACK
        assert not capture.browser_lost and not lost.is_set() and not lost_events(capture)
        capture.stop()
    finally:
        client.close()
        peer.close()


@pytest.mark.parametrize("order", ["navigating-status-first", "navigating-span-first", "missed-navigation-span-first"])
def test_a_reloaded_documents_new_span_still_arms_the_watchdog(broker, order):
    """The new document's span may reach the broker before the status that
    names its documentGeneration; it is that document's span, not the old one's.

    The extension publishes present as soon as the next document's frame
    connects, with no generation yet (background.js onConnect, documentFor),
    and only then tells that frame to start: that is what a navigating-then-
    span-first order looks like on the wire."""
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "rearmed-" + order)
        assert client.handle(declare("old-frame", "doc-1:source:1")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        report(peer, capture, "present", "doc-1")
        seen_document(capture, "doc-1")
        if order.startswith("navigating"):
            report(peer, capture, "navigating", "doc-1")
            wait_for(lambda: capture.streams.tracks["old-frame"].dormant)
        if order == "navigating-status-first":
            report(peer, capture, "present", "doc-2")
            seen_document(capture, "doc-2")
        elif order == "navigating-span-first":
            report(peer, capture, "present")
            wait_for(lambda: not capture.streams.departing)
        assert client.handle(declare("new-frame", "doc-2:source:1")) == ACK
        assert client.handle(pcm("new-frame", 480, offset=1000)) == ACK
        if order != "navigating-status-first":
            report(peer, capture, "present", "doc-2")
            seen_document(capture, "doc-2")
        peer.inventory((3,))
        assert capture.streams.tracks["old-frame"].dormant
        assert not capture.streams.tracks["new-frame"].dormant
        # The new recorder freezes: it is still caught at exactly 45 s.
        (transport,) = service._audio_transports
        clock.now = PCM_STALE_AFTER - 0.001
        service.tick()
        assert not transport.shut_down.is_set()
        clock.now = PCM_STALE_AFTER
        service.tick()
        assert transport.shut_down.is_set()
        durable(lost, "the fallback did not commit")
        assert len(lost_events(capture)) == 1
        capture.stop()
    finally:
        client.close()
        peer.close()


def test_dormant_span_pcm_is_still_recorded_but_does_not_rearm(broker):
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "late-buffer")
        assert client.handle(declare("old-frame", "doc-1:source:1")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        report(peer, capture, "navigating", "doc-1")
        wait_for(lambda: capture.streams.tracks["old-frame"].dormant)
        # The old document posts a final buffer after navigating was published.
        assert client.handle(pcm("old-frame", 480, sequence=1, offset=10)) == ACK
        old = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == "old-frame")
        assert old["frames"] == 960, "a dormant span's PCM is still recorded"
        assert capture.streams.tracks["old-frame"].dormant and not capture.remote_open()
        report(peer, capture, "present", "doc-2")
        peer.inventory((3,))
        seen_document(capture, "doc-2")
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set()
        assert client.handle(declare("guest", "doc-2:source:1")) == ACK
        assert not capture.browser_lost and not lost.is_set()
        capture.stop()
    finally:
        client.close()
        peer.close()


@pytest.mark.parametrize("reload", ["fast", "slow"])
def test_a_span_the_departing_document_declares_after_navigating_is_retired(broker, reload):
    """tabs.onUpdated loading publishes navigating while the old document still
    runs: its obsolete frame's packets are still forwarded until the next one
    commits, so it can declare one more span after T1. That span never gets
    trackEnd either. Fast: the page returns before any fallback. Slow: the
    late span arrives on fallback and the browser source is restored later."""
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "departing-" + reload)
        assert client.handle(declare("old-frame", "doc-1:source:1")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        report(peer, capture, "present", "doc-1")
        seen_document(capture, "doc-1")
        report(peer, capture, "navigating", "doc-1")
        wait_for(lambda: capture.streams.departing)
        if reload == "slow":
            capture.fallback()
        assert client.handle(declare("old-late", "doc-1:source:2")) == ACK
        assert client.handle(pcm("old-late", 480, offset=5)) == ACK
        assert capture.streams.tracks["old-late"].dormant
        report(peer, capture, "present")  # The next document's frame connected.
        wait_for(lambda: not capture.streams.departing)
        report(peer, capture, "present", "doc-2")
        peer.inventory((3,))
        seen_document(capture, "doc-2")
        assert client.handle(declare("new-frame", "doc-2:source:1")) == ACK
        assert client.handle(pcm("new-frame", 480, offset=1000)) == ACK
        assert not capture.streams.tracks["new-frame"].dormant
        if reload == "slow":
            capture.restore()
            assert not capture.browser_lost
        # The guest leaves: the only live span ends, a healthy quiet.
        assert client.handle(end("new-frame")) == ACK
        assert not capture.remote_open()
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        (transport,) = service._audio_transports
        assert not transport.shut_down.is_set(), "the departed document's late span is not a running recorder"
        assert client.handle(declare("guest-back", "doc-2:source:2")) == ACK
        assert not capture.browser_lost
        assert len(lost_events(capture)) == (1 if reload == "slow" else 0)
        capture.stop()
    finally:
        client.close()
        peer.close()


def test_a_frozen_companions_transport_is_closed_on_a_control_loss_fallback(broker):
    """D57: the stale-PCM watchdog keeps running on a control-loss fallback.
    The frozen companion's transport stays attached, so without the close no
    resume could ever be asked."""
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    try:
        capture = negotiated(client, service, "frozen-on-fallback")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        # Control was lost and policy committed computer sound; the audio
        # transport is still attached but nothing arrives on it any more.
        capture.fallback()
        durable(lost, "the fallback did not commit")
        assert capture.browser_lost and capture.audio_connected
        (transport,) = service._audio_transports
        clock.now = PCM_STALE_AFTER - 0.001
        service.tick()
        assert not transport.shut_down.is_set()
        clock.now = PCM_STALE_AFTER
        service.tick()
        assert transport.shut_down.is_set()
        # The broker's ordinary EOF teardown: detached, still on the fallback.
        wait_for(lambda: not service._audio_transports and not capture.audio_connected)
        assert capture.browser_lost and not capture.finished and len(lost_events(capture)) == 1
        assert capture.streams.tracks["guest"].ended, "the closed transport's span is retired"
        capture.stop()
    finally:
        client.close()


def test_an_old_documents_late_span_stays_dormant_after_the_window_closes(broker):
    """D61: a span that names its document is dormant exactly while that
    generation has departed, whenever it arrives; a new document's span arms."""
    service, path, clock, lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "late-old-document")
        assert client.handle(dict(declare("old-frame", "doc-1:source:1"), documentGeneration="doc-1")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        report(peer, capture, "present", "doc-1")
        seen_document(capture, "doc-1")
        report(peer, capture, "navigating", "doc-1")
        wait_for(lambda: capture.streams.departing)
        assert capture.departed_documents == {"doc-1"} and not capture.remote_open()
        report(peer, capture, "present", "doc-2")
        peer.inventory((3,))
        seen_document(capture, "doc-2")
        assert not capture.streams.departing
        # The old document's last span reaches the broker after the window closed.
        assert client.handle(dict(declare("old-late", "doc-1:source:2"), documentGeneration="doc-1")) == ACK
        assert client.handle(pcm("old-late", 480, offset=5)) == ACK
        old = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == "old-late")
        assert old["frames"] == 480 and "documentGeneration" not in old, "its PCM is still recorded"
        assert not capture.streams.tracks["old-late"].dormant, "the declaration-order flag is not what decides"
        assert not capture.remote_open()
        (transport,) = service._audio_transports
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        assert not transport.shut_down.is_set(), "a departed document's span is not a running recorder"
        # The new document's span arms as usual, and a track never moves the current generation.
        assert client.handle(dict(declare("new-frame", "doc-2:source:1"), documentGeneration="doc-2")) == ACK
        assert client.handle(pcm("new-frame", 480, offset=1000)) == ACK
        assert capture.remote_open() and capture.document_generation == "doc-2"
        clock.now = PCM_STALE_AFTER * 11 - 0.001
        service.tick()
        assert not transport.shut_down.is_set()
        clock.now = PCM_STALE_AFTER * 11
        service.tick()
        assert transport.shut_down.is_set()
        durable(lost, "the fallback did not commit")
        assert len(lost_events(capture)) == 1
        capture.stop()
    finally:
        client.close()
        peer.close()


def test_navigating_departs_every_open_spans_own_generation(broker):
    """A span of a document the app never saw named (a missed reload) still
    departs when the tab navigates; it stays dormant once the tab is back."""
    service, path, clock, _lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "navigating-open-spans")
        report(peer, capture, "present", "doc-1")
        seen_document(capture, "doc-1")
        assert client.handle(dict(declare("b", "doc-2:source:1"), documentGeneration="doc-2")) == ACK
        assert client.handle(pcm("b", 480)) == ACK
        assert capture.remote_open()
        report(peer, capture, "navigating")
        wait_for(lambda: capture.streams.departing)
        report(peer, capture, "present")
        wait_for(lambda: not capture.streams.departing)
        assert capture.departed_documents == {"doc-1", "doc-2"} and not capture.remote_open()
        (transport,) = service._audio_transports
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        assert not transport.shut_down.is_set()
        capture.stop()
    finally:
        client.close()
        peer.close()


@pytest.mark.parametrize("generation", ["", "é" * 101, 7], ids=["empty", "201-bytes", "number"])
def test_an_invalid_span_generation_counts_as_absent(broker, generation):
    """Such a span follows the declaration-order rules: declared in the
    departing window it is born dormant, and the window's close never wakes it."""
    service, path, clock, _lost = broker
    client = Settled(BrowserAudioProxy(path=path), service)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "invalid-generation")
        report(peer, capture, "present", "doc-1")
        seen_document(capture, "doc-1")
        report(peer, capture, "navigating", "doc-1")
        wait_for(lambda: capture.streams.departing)
        assert client.handle(dict(declare("late", "doc-1:source:1"), documentGeneration=generation)) == ACK
        assert client.handle(pcm("late", 480)) == ACK
        span = capture.streams.tracks["late"]
        assert span.document is None and span.dormant
        report(peer, capture, "present", "doc-2")
        peer.inventory((3,))
        seen_document(capture, "doc-2")
        assert generation not in capture.departed_documents and not capture.remote_open()
        (transport,) = service._audio_transports
        clock.now = PCM_STALE_AFTER * 10
        service.tick()
        assert not transport.shut_down.is_set()
        capture.stop()
    finally:
        client.close()
        peer.close()
