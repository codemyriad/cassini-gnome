# SPDX-License-Identifier: GPL-3.0-or-later
"""G7: a frozen companion keeps its sockets open; deadlines on an injected clock close them."""

import json
import logging
import socket
import threading
import time

import pytest
from broker_fixture import stop_broker
from test_browser_capture import Tap
from test_browser_service import wait_for
from test_browser_sessions import Extension
from test_capture_record import declare, pcm

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.browser_service import PCM_STALE_AFTER, WRITE_DEADLINE, BrowserService, send_packet

ACK = dict(type="ack")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def broker(tmp_path, monkeypatch):
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


def negotiated(client, service, identifier):
    reply = client.handle(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId=identifier,
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


def me_frames(capture):
    identifier = capture.native_taps["microphone"][0]
    return next(t["frames"] for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)


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
    service.batch_timeout = 1
    client = BrowserAudioProxy(path=path)
    peer = Extension(path, (3,))
    try:
        capture = negotiated(client, service, "stuck-stop")
        state = dict(recording=True, tabId=3, captureId="stuck-stop", path=str(capture.directory))
        send_packet(peer.stream, dict(type="status", recording=True, sessions=[state]))
        (browser,) = service.browsers.values()
        wait_for(lambda: browser.states.get(3, {}).get("captureId") == "stuck-stop")
        jam(browser)

        generation = service._stop_generation
        resumed = threading.Event()
        policy = service.policy

        def observed():
            if service._stop_generation > generation and not service._stopping_all.is_set():
                resumed.set()
            policy()

        monkeypatch.setattr(service, "policy", observed)
        began = time.monotonic()
        result = service.stop_all()
        assert time.monotonic() - began < service.batch_timeout + 1
        assert str(capture.directory) in result["stoppedPaths"]
        manifest = json.loads((capture.directory / "session.json").read_text())
        assert manifest["state"] == "completed", manifest.get("problem")
        # A future stuck in a socket write used to hold _stopping_all forever,
        # pausing every fallback and automatic start behind one frozen peer.
        assert resumed.wait(service.batch_timeout + WRITE_DEADLINE)
        assert browser.write_started is not None, "The peer is still not reading"
    finally:
        client.close()
        peer.close()


def test_frozen_audio_transport_falls_back_and_keeps_me(broker):
    service, path, clock, lost = broker
    client = BrowserAudioProxy(path=path)
    try:
        capture = negotiated(client, service, "frozen")
        assert client.handle(declare("guest", "receiver")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        tap = capture.microphone
        tap.append(b"\x01\x00" * 480, 200)
        before = me_frames(capture)
        # Freeze: the companion neither sends nor reads; its socket stays open.
        clock.now = PCM_STALE_AFTER - 0.5
        service.tick()
        assert not lost.is_set() and not capture.browser_lost
        clock.now = PCM_STALE_AFTER
        service.tick()
        assert lost.wait(5)
        (event,) = lost_events(capture)
        assert event["source"] == "system"
        assert capture.native_taps.keys() == {"microphone", "system"}
        tap.append(b"\x02\x00" * 480, 300)
        assert me_frames(capture) > before
        assert not capture.finished
        service.command("capture:frozen", "stop")
        manifest = json.loads((capture.directory / "session.json").read_text())
        # Saved; the frozen guest's announced span honestly never ended.
        assert manifest["state"] == "interrupted" and manifest["problemKind"] == "capture"
        assert manifest["tracks"][-1]["kind"] == "system"
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
        assert lost.wait(5)
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
    client = BrowserAudioProxy(path=path)
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
    client = BrowserAudioProxy(path=path)
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
    client = BrowserAudioProxy(path=path)
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
    client = BrowserAudioProxy(path=path)
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
        assert lost.wait(5)
        assert len(lost_events(capture)) == 1
        capture.stop()
    finally:
        client.close()


def test_a_recorder_waiting_out_its_longest_retry_is_not_cut_over(broker):
    service, path, clock, lost = broker
    client = BrowserAudioProxy(path=path)
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
    client = BrowserAudioProxy(path=path)
    try:
        capture = negotiated(client, service, "reloaded")
        assert client.handle(declare("old-frame", "receiver")) == ACK
        assert client.handle(pcm("old-frame", 480)) == ACK
        # The page reloads: the old document's recorder dies without trackEnd,
        # policy falls back, and the new document's span restores the browser.
        capture.fallback()
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


def test_the_brokers_own_stall_is_not_blamed_on_the_peer(broker):
    service, path, clock, lost = broker
    client = BrowserAudioProxy(path=path)
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
    client = BrowserAudioProxy(path=path)
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
        assert lost.wait(5)
    finally:
        client.close()


def test_a_capture_without_multistream_control_is_never_cut_over(broker):
    service, path, clock, lost = broker
    client = BrowserAudioProxy(path=path)
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
    client = BrowserAudioProxy(path=path)
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
    client = BrowserAudioProxy(path=path)
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
        assert lost.wait(5) and lost_events(capture)
    finally:
        client.close()
