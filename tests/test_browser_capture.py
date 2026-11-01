# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent writer ownership and per-session microphone choice, without devices."""

import base64
import json
import struct
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest
from broker_fixture import stop_broker

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.browser_service import BrowserService, request


class Tap:
    instances = []

    def __init__(self, kind, origin, append, failed, *, level):
        self.kind, self.append = kind, append
        self.started = self.stopped = False
        self.instances.append(self)

    def start(self):
        self.started = True
        self.append(struct.pack("<hhhh", 100, 200, 300, 400), 100)

    def stop(self):
        self.stopped = True


@pytest.fixture
def server(tmp_path):
    Tap.instances = []
    # This fixture owns synthetic PCM, not a desktop main loop. Automatic-start
    # tests must not start notify_recording's persistent GLib loop on a worker.
    service = BrowserService(
        tmp_path / "config.json",
        capture_parent=tmp_path / "library",
        tap_factory=Tap,
        notifier=lambda _title, _stop: None,
    )
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    yield service, path, tmp_path / "library"
    stop_broker(service, worker, path)


def start(client, identifier, *, microphone=True):
    return client.handle(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId=identifier,
            timelineOriginMs=int(time.time() * 1000),
            microphone=microphone,
            title=identifier,
        )
    )


def remote(client, identifier):
    client.handle(dict(type="track", id=identifier, label="Other", kind="remote", sampleRate=48000, channels=1))
    client.handle(
        dict(type="audio", id=identifier, sequence=0, offsetMs=200, pcm=base64.b64encode(b"\x01\x00" * 4).decode())
    )


def test_broker_preserves_receiver_identity_across_new_wire_spans(server):
    _service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        started = start(client, "receiver-spans", microphone=False)
        for index, source in enumerate(("receiver-a", "receiver-a", "receiver-b")):
            identifier = f"span-{index}"
            client.handle(
                dict(
                    type="track",
                    id=identifier,
                    label="Guest",
                    kind="remote",
                    sampleRate=48000,
                    channels=1,
                    sourceTrackId=source,
                )
            )
            client.handle(dict(type="audio", id=identifier, sequence=0, offsetMs=index * 100, pcm="AQACAA=="))
        client.handle(dict(type="stop"))
        from pathlib import Path

        manifest = json.loads((Path(started["path"]) / "session.json").read_text())
        assert [t["id"] for t in manifest["tracks"]] == ["span-0", "span-1", "span-2"]
        assert [t["sourceTrackId"] for t in manifest["tracks"]] == ["receiver-a", "receiver-a", "receiver-b"]
        assert [t["frames"] for t in manifest["tracks"]] == [2, 2, 2]
    finally:
        client.close()


def test_broker_keeps_negotiated_source_io_errors_scoped_and_retries_a_fresh_span(server, monkeypatch):
    from test_capture_record import declare, pcm

    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        reply = client.handle(
            dict(
                type="start",
                independent=True,
                captureId="source-error",
                timelineOriginMs=int(time.time() * 1000),
                microphone=False,
                features=["source-outcomes-v1"],
            )
        )
        assert reply["features"] == ["source-outcomes-v1"]
        client.handle(declare("broken", "receiver-a"))
        capture = service.captures["source-error"]

        def no_space(_data):
            raise OSError(28, "Generated ENOSPC")

        monkeypatch.setattr(capture.host.session.writers["broken"], "append", no_space)
        failed = client.handle(pcm("broken", 48000))
        assert failed["scope"] == "source" and failed["code"] == "io"
        assert not capture.finished and not capture.stopping
        assert client.handle(declare("healthy", "receiver-b")) == dict(type="ack")
        assert client.handle(pcm("healthy", 48000)) == dict(type="ack")
        assert client.handle(declare("retry", "receiver-a")) == dict(type="ack")
        assert client.handle(pcm("retry", 48000, offset=1000)) == dict(type="ack")
        refused = client.handle(dict(type="stop", captureId="stale-capture"))
        assert refused["scope"] == "command" and not capture.finished
        client.handle(dict(type="stop", captureId="source-error"))
        assert capture.finished and capture.host.session.manifest["problemKind"] == "io"
        before = (capture.directory / "session.json").read_bytes()
        assert client.handle(pcm("healthy", 1, sequence=1, offset=1000))["scope"] == "source"
        assert (capture.directory / "session.json").read_bytes() == before
    finally:
        client.close()


def test_only_pcm_written_to_a_browser_span_proves_liveness(tmp_path, monkeypatch):
    from test_capture_record import declare, pcm

    from cassini_talk.browser_capture import BrowserCapture

    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Tap)
    capture.start(dict(type="start", captureId="live", timelineOriginMs=int(time.time() * 1000), microphone=True))
    try:
        assert not capture.browser_ready_since(recorded=True), "Native microphone is not browser evidence"
        capture.handle(declare("remote"))
        capture.handle(pcm("remote", 480))
        assert capture.browser_ready_since(recorded=True)
        capture.select(True, False)
        capture.handle(pcm("remote", 480, sequence=1, offset=10))
        assert capture.browser_ready_since() and not capture.browser_ready_since(recorded=True)
        capture.select(True, True)
        # Reopening a span must not inherit the discarded packet's freshness.
        assert not capture.browser_ready_since(recorded=True)
        at = capture.host.session.manifest["events"][-1]["atMs"]
        capture.handle(pcm("remote", 480, sequence=2, offset=at))
        assert capture.browser_ready_since(recorded=True)
        capture.fallback()
        capture.handle(pcm("remote", 480, sequence=3, offset=at + 10))
        assert capture.browser_ready_since() and not capture.browser_ready_since(recorded=True)
    finally:
        capture.stop()


def test_independent_native_host_writers_copy_only_the_selected_microphone(server):
    service, path, root = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        first = start(one, "first")
        second = start(two, "second", microphone=False)
        assert first["path"] != second["path"]
        assert len(Tap.instances) == 1 and Tap.instances[0].started
        remote(one, "remote-one")
        remote(two, "remote-two")
        assert len([s for s in request(path=path)["statuses"] if s["recording"]]) == 2
        one.handle(dict(type="stop"))
        states = request(path=path)["statuses"]
        assert [s["captureId"] for s in states if s["recording"]] == ["second"]
        assert Tap.instances[0].stopped
        two.handle(
            dict(
                type="audio",
                id="remote-two",
                sequence=1,
                offsetMs=200 + 4 * 1000 / 48000,
                pcm=base64.b64encode(b"\x02\x00" * 4).decode(),
            )
        )
        two.handle(dict(type="stop"))
        manifests = [json.loads((p / "session.json").read_text()) for p in sorted(root.iterdir())]
        assert len(manifests) == 2 and all(m["state"] == "completed" for m in manifests)
        first = next(m for m in manifests if m["title"] == "first")
        second = next(m for m in manifests if m["title"] == "second")
        assert [t["kind"] for t in first["tracks"]] == ["microphone", "remote"]
        assert [t["kind"] for t in second["tracks"]] == ["remote"]
        assert first["tracks"][0]["labelSource"] == "device"
        assert first["tracks"][0]["startOffsetMs"] == 100
        assert second["tracks"][0]["frames"] == 8
        for folder in root.iterdir():
            metadata = json.loads((folder / "session.json").read_text())
            for track in metadata["tracks"]:
                with wave.open(str(folder / track["file"])) as audio:
                    assert audio.getnframes() == track["frames"]
        assert len(service.recordings) == 2
    finally:
        one.close()
        two.close()


def test_audio_peer_eof_falls_back_only_its_own_capture_and_keeps_the_microphone(server):
    service, path, root = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        first = start(one, "first")
        start(two, "second", microphone=False)
        remote(one, "remote-one")
        remote(two, "remote-two")
        one.close()
        end = time.monotonic() + 2
        while time.monotonic() < end and not service.captures["first"].browser_lost:
            time.sleep(0.01)
        assert [s["captureId"] for s in request(path=path)["statuses"] if s["recording"]] == ["first", "second"]
        from pathlib import Path

        value = json.loads((Path(first["path"]) / "session.json").read_text())
        assert value["state"] == "recording"
        assert value["events"][-1] == dict(
            atMs=value["events"][-1]["atMs"], type="othersSource", source="system", reason="lost"
        )
        assert value["tracks"][-1]["kind"] == "system"
        assert value["tracks"][-1]["startOffsetMs"] == value["events"][-1]["atMs"]
        assert not service.captures["first"].microphone.stopped
        assert not service.captures["second"].browser_lost
        service.captures["first"].stop()
        two.handle(dict(type="stop"))
    finally:
        one.close()
        two.close()


def test_duplicate_identity_is_rejected_without_an_orphan_folder(server):
    _, path, root = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        start(one, "same", microphone=False)
        with pytest.raises(ValueError, match="identity already exists"):
            start(two, "same", microphone=False)
        assert len(list(root.iterdir())) == 1
        one.handle(dict(type="stop"))
    finally:
        one.close()
        two.close()


def test_two_stop_requests_join_one_native_finalization(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "one")
        capture = service.captures["one"]
        entered, release = threading.Event(), threading.Event()
        original = capture.microphone.stop

        def stop():
            entered.set()
            assert release.wait(2)
            original()

        capture.microphone.stop = stop
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(capture.stop)
            assert entered.wait(1)
            second = pool.submit(capture.stop)
            release.set()
            assert first.result(2)["path"] == second.result(2)["path"]
        assert capture.finished
    finally:
        client.close()


def test_rejected_browser_start_uses_its_own_system_fallback_without_forcing_microphone(server):
    from test_browser_sessions import Extension

    service, path, _ = server
    peer = Extension(path, (3,))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path, microphone=False)
            peer.answer(peer.receive(), ok=False)
            result = pending.result(2)
        assert result["attempted"] == 1 and not result["failures"]
        active = [s for s in result["statuses"] if s["recording"]]
        assert len(active) == 1
        capture = service.captures[active[0]["captureId"]]
        assert capture.microphone is None and set(capture.native_taps) == {"system"}
        value = json.loads((capture.directory / "session.json").read_text())
        assert value["events"] == [
            dict(atMs=0, type="othersSource", source="system", reason="missing"),
            dict(atMs=0, type="selection", me=False, others=True, reason="start"),
        ]
        assert [t["kind"] for t in value["tracks"]] == ["system"]
        assert value["tracks"][0]["startOffsetMs"] == 0
        request("stop-all", path=path)
        assert capture.finished
    finally:
        peer.close()


def test_rpc_selection_stops_only_its_sources_and_retains_browser_span_identity(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "selection")
        remote(client, "remote")
        capture = service.captures["selection"]
        original = capture.microphone
        request("selection", path=path, directory=str(capture.directory), me=False, others=True)
        assert original.stopped and capture.microphone is None
        selected = request("selection", path=path, directory=str(capture.directory), me=True, others=False)
        assert capture.microphone is not original and not capture.microphone.stopped
        first_remote = next(t for t in capture.host.session.manifest["tracks"] if t["kind"] == "remote")
        assert first_remote["ended"]
        frozen = (capture.directory / first_remote["file"]).read_bytes()
        offset = selected["events"][-1]["atMs"] + 10
        client.handle(
            dict(type="audio", id="remote", sequence=1, offsetMs=offset, pcm=base64.b64encode(b"\x02\x00" * 4).decode())
        )
        selected = request("selection", path=path, directory=str(capture.directory), me=True, others=True)
        client.handle(
            dict(
                type="audio",
                id="remote",
                sequence=2,
                offsetMs=selected["events"][-1]["atMs"] + 10,
                pcm=base64.b64encode(b"\x03\x00" * 4).decode(),
            )
        )
        remotes = [t for t in capture.host.session.manifest["tracks"] if t["kind"] == "remote"]
        assert len(remotes) == 2 and remotes[0]["id"] != remotes[1]["id"]
        assert remotes[0]["sourceTrackId"] == remotes[1]["sourceTrackId"] == "remote"
        assert (capture.directory / first_remote["file"]).read_bytes() == frozen
        client.handle(dict(type="stop"))
    finally:
        client.close()


def test_source_limit_failure_keeps_last_applied_choice_and_tracks(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "limit", microphone=False)
        remote(client, "remote")
        capture = service.captures["limit"]
        with capture.lock:
            for index in range(63):
                capture.host.session.add_track(f"ended-{index}", "Old", "remote", 48000, 1)
                capture.host.session.end_track(f"ended-{index}")
        before = (capture.directory / "session.json").read_bytes()
        with pytest.raises(ValueError, match="64 sources"):
            request("selection", path=path, directory=str(capture.directory), me=True, others=False)
        assert capture.me is False and capture.others is True and capture.microphone is None
        assert capture.streams.tracks["remote"].recorded == "remote"
        assert (capture.directory / "session.json").read_bytes() == before
        client.handle(dict(type="stop"))
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["track-limit-race", "native-write"])
@pytest.mark.parametrize("change", ["selection", "fallback"])
def test_rejected_change_restores_browser_pcm_after_stream_selection(server, monkeypatch, failure, change):
    from cassini_talk.recording_session import ProtocolError

    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    entered, release = threading.Event(), threading.Event()

    class ReadyTap(Tap):
        def start(self):
            entered.set()
            assert release.wait(3)
            super().start()

    try:
        start(client, "rollback", microphone=False)
        remote(client, "remote")
        capture = service.captures["rollback"]
        capture.tap_factory = ReadyTap
        session = capture.host.session
        if failure == "track-limit-race":
            with capture.lock:
                for i in range(62):
                    session.add_track(f"ended-{i}", "Old", "remote", 48000, 1)
                    session.end_track(f"ended-{i}")
        else:
            add_native = capture._add_native

            def fail_after_add(*args, **kwargs):
                add_native(*args, **kwargs)
                raise OSError("Native WAV write failed")

            monkeypatch.setattr(capture, "_add_native", fail_after_add)
        with ThreadPoolExecutor(1) as pool:
            changing = (
                pool.submit(capture.select, True, False) if change == "selection" else pool.submit(capture.fallback)
            )
            assert entered.wait(2)
            if failure == "track-limit-race":
                # The audio connection remains active during native readiness.
                remote(client, "late")
                assert len(session.writers) == 64
            before = (capture.directory / "session.json").read_bytes()
            files = set(capture.directory.iterdir())
            release.set()
            with pytest.raises((ProtocolError, OSError)):
                changing.result(3)
        assert capture.me is False and capture.others is True and not capture.browser_lost
        assert not capture.finished and not capture.native_taps
        assert capture.streams.me is False and capture.streams.others is True and capture.streams.browser
        assert capture.streams.tracks["remote"].recorded == "remote"
        assert (capture.directory / "session.json").read_bytes() == before
        assert set(capture.directory.iterdir()) == files
        assert all(t.stopped for t in Tap.instances)
        client.handle(
            dict(
                type="audio",
                id="remote",
                sequence=1,
                offsetMs=200 + 4 * 1000 / 48000,
                pcm=base64.b64encode(b"\x02\x00" * 4).decode(),
            )
        )
        assert next(t for t in session.manifest["tracks"] if t["id"] == "remote")["frames"] == 8
        client.handle(dict(type="stop"))
        track = next(t for t in session.manifest["tracks"] if t["id"] == "remote")
        with wave.open(str(capture.directory / track["file"])) as wav:
            assert wav.readframes(wav.getnframes()) == b"\x01\x00" * 4 + b"\x02\x00" * 4
    finally:
        release.set()
        client.close()


def test_stop_before_missing_browser_fallback_begins_cannot_create_a_late_folder(server, monkeypatch):
    from test_browser_sessions import Extension

    from cassini_talk.browser_capture import BrowserCapture

    service, path, root = server
    peer = Extension(path, (3,))
    entered, release = threading.Event(), threading.Event()
    original = BrowserCapture.start

    def delayed(capture, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(capture, *args, **kwargs)

    monkeypatch.setattr(BrowserCapture, "start", delayed)
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path, microphone=False, groupId="fallback-cancel")
            peer.answer(peer.receive(), ok=False)
            assert entered.wait(2)
            request("stop-all", path=path, cancelGroups=["fallback-cancel"])
            release.set()
            assert pending.result(2)["cancelled"]
        assert not list(root.glob("Recording-*"))
        assert not any(s.get("recording") for s in request(path=path)["statuses"])
        assert not service.captures
    finally:
        release.set()
        peer.close()


def bind_capture(peer, capture, tab=3):
    from test_browser_service import wait_for

    from cassini_talk.browser_service import send_packet

    peer.sessions[tab] = dict(recording=True, tabId=tab, captureId=capture.capture_id, path=str(capture.directory))
    send_packet(peer.stream, dict(type="status", recording=True, sessions=list(peer.sessions.values())))
    wait_for(
        lambda: any(
            s.get("tabId") == tab and s.get("captureId") == capture.capture_id
            for s in request(path=peer.path)["statuses"]
        )
    )


def test_a_call_stopped_while_its_browser_is_away_stays_stopped(server):
    from test_browser_service import wait_for
    from test_browser_sessions import Extension

    service, path, _ = server
    peer = Extension(path, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=path)
    start(client, "away")
    capture = service.captures["away"]
    bind_capture(peer, capture)
    peer.close()
    client.close()
    wait_for(lambda: capture.browser_lost and not service.browsers)
    request("stop-all", path=path)
    wait_for(lambda: capture.finished)
    # The extension's new worker reports the call that is still open.
    peer = Extension(path, (3,), instance="4242:99")
    try:
        wait_for(lambda: 3 in next(iter(service.browsers.values())).attempted)
    finally:
        peer.close()


def test_independent_call_hangup_stops_without_system_fallback(server):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    from cassini_talk.browser_service import send_packet

    service, path, _ = server
    peer = Extension(path, (3,))
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "hangup")
        remote(client, "remote")
        capture = service.captures["hangup"]
        bind_capture(peer, capture)
        send_packet(peer.stream, dict(type="meetings", meetings=[dict(meeting(), sourceCount=0)]))
        wait_for(lambda: "hangup" in service._capture_missing_since)
        assert not capture.browser_lost and not capture.finished
        assert {tap.kind for tap in Tap.instances} == {"microphone"}
        with service.lock:
            service._capture_missing_since["hangup"] = time.monotonic() - 2.01
        request(path=path)  # Busy accept traffic must also advance policy.
        command = peer.receive()
        assert command["action"] == "stop"
        peer.answer(command)
        wait_for(lambda: capture.finished)
        manifest = json.loads((capture.directory / "session.json").read_text())
        assert manifest["state"] == "completed"
        assert not any(t["kind"] == "system" for t in manifest["tracks"])
        assert capture.microphone.stopped
    finally:
        client.close()
        peer.close()


def test_reload_fallback_survives_return_but_stops_when_call_does_not_return(server):
    from test_browser_service import wait_for
    from test_browser_sessions import Extension

    service, path, _ = server
    peer = Extension(path, (3,))
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "reload")
        remote(client, "remote")
        capture = service.captures["reload"]
        bind_capture(peer, capture)
        peer.inventory(())
        command = peer.receive()
        assert command["action"] == "stop"
        peer.answer(command)
        wait_for(lambda: capture.browser_lost)
        assert not capture.finished and set(capture.native_taps) == {"microphone", "system"}
        peer.inventory((3,))
        wait_for(lambda: "reload" not in service._capture_missing_since)
        assert capture.browser_lost  # No automatic switch back.
        peer.inventory(())
        wait_for(lambda: "reload" in service._capture_missing_since)
        with service.lock:
            service._capture_missing_since["reload"] = time.monotonic() - 120.01
        service.policy()
        wait_for(lambda: capture.finished)
        assert all(tap.stopped for tap in Tap.instances)
        assert json.loads((capture.directory / "session.json").read_text())["state"] == "completed"
    finally:
        client.close()
        peer.close()


def test_disconnected_fallback_is_saved_after_bounded_browser_return_grace(server):
    from test_browser_service import wait_for
    from test_browser_sessions import Extension

    service, path, _ = server
    peer = Extension(path, (3,))
    client = BrowserAudioProxy(path=path)
    start(client, "disconnected")
    capture = service.captures["disconnected"]
    bind_capture(peer, capture)
    peer.close()
    wait_for(lambda: not service.browsers)
    client.close()
    wait_for(lambda: capture.browser_lost)
    service.policy()
    assert not capture.finished
    with service.lock:
        service._capture_missing_since["disconnected"] = time.monotonic() - 120.01
    service.policy()
    wait_for(lambda: capture.finished)
    assert all(tap.stopped for tap in Tap.instances)
    assert json.loads((capture.directory / "session.json").read_text())["state"] == "completed"


def test_unbound_audio_client_is_not_mistaken_for_a_disconnected_call(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "unbound")
        capture = service.captures["unbound"]
        with service.lock:
            service._capture_missing_since["unbound"] = time.monotonic() - 100
        service.policy()
        assert not capture.finished and not capture.browser_lost
        assert "unbound" not in service._capture_missing_since
        client.handle(dict(type="stop"))
        assert capture.finished
    finally:
        client.close()


def test_unobserved_audio_disconnect_still_starts_bounded_fallback_grace(server):
    from test_browser_service import wait_for

    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "unobserved-loss")
        remote(client, "remote")
        capture = service.captures["unobserved-loss"]
        assert "unobserved-loss" not in service._capture_observed
        client.close()
        wait_for(lambda: capture.browser_lost)
        service.policy()
        assert "unobserved-loss" in service._capture_missing_since
        assert service._capture_missing_since["unobserved-loss"] == capture.browser_lost_since
        assert not capture.finished
        with service.lock:
            service._capture_missing_since.pop("unobserved-loss")
            capture.browser_lost_since = time.monotonic() - 120.01
        service.policy()
        wait_for(lambda: capture.finished)
        assert all(tap.stopped for tap in Tap.instances)
        assert json.loads((capture.directory / "session.json").read_bytes())["state"] == "completed"
    finally:
        client.close()


def test_initial_system_fallback_expires_when_control_left_before_first_policy(server):
    from unittest.mock import patch

    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    service, path, _ = server
    # Deterministically put both start and disconnect between policy ticks.
    with patch.object(service, "policy"):
        peer = Extension(path, (3,))
        try:
            identifier = request(path=path)["meetings"][0]["id"]
            snapshot = service.start_fallback(identifier, meeting())
            capture = service.captures[snapshot["captureId"]]
            peer.close()
            wait_for(lambda: not service.browsers)
        finally:
            peer.close()
    key = capture.capture_id
    assert key not in service._capture_observed and capture.browser_lost
    with service.lock:
        capture.browser_lost_since = time.monotonic() - 120.01
    service.policy()
    wait_for(lambda: capture.finished)
    assert all(tap.stopped for tap in Tap.instances)
    assert json.loads((capture.directory / "session.json").read_bytes())["state"] == "completed"


def test_lost_page_draining_browser_audio_uses_recovery_grace_not_call_end(server):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    from cassini_talk.browser_service import send_packet

    service, path, _ = server
    peer = Extension(path, (3,))
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "draining")
        capture = service.captures["draining"]
        bind_capture(peer, capture)
        peer.sessions[3]["stopping"] = True  # Extension is draining a lost frame.
        send_packet(peer.stream, dict(type="status", recording=True, sessions=list(peer.sessions.values())))
        wait_for(lambda: next(iter(service.browsers.values())).states[3].get("stopping"))
        send_packet(peer.stream, dict(type="meetings", meetings=[dict(meeting(), sourceCount=0)]))
        peer.answer(peer.receive())
        wait_for(lambda: capture.browser_lost)
        assert not capture.finished and set(capture.native_taps) == {"microphone", "system"}
        with service.lock:
            service._capture_missing_since["draining"] = time.monotonic() - 2.01
        service.policy()
        assert not capture.finished and not capture.stop_requested.is_set()
        capture.stop()
    finally:
        client.close()
        peer.close()


def test_permission_evidence_requires_native_pcm_not_remote_audio(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "permissions", microphone=False)
        remote(client, "remote-only")
        assert service.captures["permissions"].snapshot()["permittedSources"] == []
        capture = service.captures["permissions"]
        capture.select(True, True)
        identifier, microphone = capture.native_taps["microphone"]
        track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
        microphone.append(b"\1\0" * 4, track["startOffsetMs"] + 100)
        assert capture.snapshot()["permittedSources"] == ["microphone"]
        capture.fallback()
        identifier, system = capture.native_taps["system"]
        track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
        system.append(b"\1\0" * 4, track["startOffsetMs"] + 100)
        assert capture.snapshot()["permittedSources"] == ["microphone", "system"]
    finally:
        client.close()


def test_disconnected_user_stop_keeps_original_url_and_does_not_suppress_new_meeting(server):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    from cassini_talk.browser_service import send_packet

    service, path, _ = server
    peer = Extension(path, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "original-url")
        capture = service.captures["original-url"]
        bind_capture(peer, capture)
        peer.close()
        wait_for(lambda: not service.browsers)
        request("stop-all", path=path)
        assert service.stopped_calls[("4242:99", 3)][0] == meeting()["url"]
        peer = Extension(path, (), instance="4242:99")
        send_packet(peer.stream, dict(type="meetings", meetings=[dict(meeting(), url="https://meeting.example/new")]))
        wait_for(lambda: request(path=path)["meetings"])
        assert ("4242:99", 3) not in service.stopped_calls
        assert not next(iter(service.browsers.values())).attempted
    finally:
        peer.close()
        client.close()


def test_disconnected_capture_stops_share_the_bounded_stop_store(server):
    from types import SimpleNamespace

    from test_browser_service import meeting

    from cassini_talk.browser_service import STOPPED_CALL_LIMIT, BrowserConnection

    service, _, _ = server
    for index in range(STOPPED_CALL_LIMIT + 7):
        identifier = str(index)
        connection = BrowserConnection(None, browser_instance="4242:99")
        connection.meetings[index] = dict(meeting(index), url="https://meeting.example/" + identifier)
        capture = SimpleNamespace(capture_id=identifier, finished=False)
        service.captures[identifier] = capture
        # Bind through the same status handler, then stop after the link is
        # absent. This exercises the disconnected insertion path, not its map.
        service._status(
            connection, dict(recording=True, sessions=[dict(tabId=index, recording=True, captureId=identifier)])
        )
        service._remember_capture_stop(capture)
        service.captures.pop(identifier)
    assert len(service.stopped_calls) == STOPPED_CALL_LIMIT
    assert ("4242:99", 0) not in service.stopped_calls
    assert ("4242:99", STOPPED_CALL_LIMIT + 6) in service.stopped_calls


def test_first_status_binds_the_call_before_its_audio_socket_opens(server):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    from cassini_talk.browser_service import send_packet

    service, path, _ = server
    peer = Extension(path, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=path)
    try:
        send_packet(
            peer.stream,
            dict(type="status", recording=True, sessions=[dict(tabId=3, recording=True, captureId="early")]),
        )
        wait_for(lambda: any(s.get("captureId") == "early" for s in request(path=path)["statuses"]))
        start(client, "early")
        # There is deliberately no second status after the socket opens.
        peer.close()
        wait_for(lambda: not service.browsers)
        request("stop-all", path=path)
        assert service.captures["early"].finished
        assert service.stopped_calls[("4242:99", 3)][0] == meeting()["url"]
    finally:
        peer.close()
        client.close()


def test_stop_all_remembers_the_user_stop_when_control_disconnects_after_its_snapshot(server, monkeypatch):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    service, path, _ = server
    peer = Extension(path, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "snapshot-race")
        capture = service.captures["snapshot-race"]
        bind_capture(peer, capture)
        original = service.command

        def disconnect(identifier, action, **kwargs):
            peer.close()
            wait_for(lambda: not service.browsers)
            return original(identifier, action, **kwargs)

        monkeypatch.setattr(service, "command", disconnect)
        request("stop-all", path=path)
        assert capture.finished
        assert service.stopped_calls[("4242:99", 3)][0] == meeting()["url"]
    finally:
        peer.close()
        client.close()


@pytest.mark.parametrize("preserve_capture", [False, True])
def test_user_stop_joins_pending_automatic_stop_and_finishes_its_capture(server, preserve_capture):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    service, path, _ = server
    peer = Extension(path, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "pending-stop")
        capture = service.captures["pending-stop"]
        bind_capture(peer, capture)
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(2) as pool:
            automatic = pool.submit(service.command, identifier, "stop", preserve_capture=preserve_capture)
            command = peer.receive()
            user = pool.submit(request, "stop-all", path=path)
            wait_for(lambda: capture.finished and ("4242:99", 3) in service.stopped_calls)
            assert service.stopped_calls[("4242:99", 3)][0] == meeting()["url"]
            # The user sees the completed local save even if the page has not
            # acknowledged the automatic/fallback stop yet.
            assert not any(s.get("recording") for s in user.result(2)["statuses"])
            if preserve_capture:
                assert not automatic.done()
            else:
                assert automatic.result(0.2)["stoppedPath"] == str(capture.directory)
            peer.answer(command)
            assert automatic.result(2)
            assert not next(iter(service.browsers.values())).pending, "A second wire Stop must not be sent"
    finally:
        peer.close()
        client.close()


def test_fresh_stop_can_be_reported_late_on_an_old_control_link(server):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    from cassini_talk.browser_service import STOPPED_CALL_REPORT_WINDOW

    service, path, _ = server
    peer = Extension(path, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "late-report")
        capture = service.captures["late-report"]
        bind_capture(peer, capture)
        with service.lock:
            connection = next(iter(service.browsers.values()))
            connection.connected_at -= STOPPED_CALL_REPORT_WINDOW + 1
        # Native fallback is still recording while this tab is briefly absent.
        capture.fallback()
        peer.inventory(())
        wait_for(lambda: not request(path=path)["meetings"])
        request("stop-all", path=path)
        peer.inventory((7,))
        wait_for(lambda: len(request(path=path)["meetings"]) == 1)
        peer.inventory((3, 7))
        wait_for(lambda: len(request(path=path)["meetings"]) == 2)
        assert service.stopped_calls[("4242:99", 3)][0] == meeting()["url"]
        assert 3 in connection.attempted
    finally:
        peer.close()
        client.close()


def test_broker_native_first_stop_keeps_a_never_opened_source_without_late_rewrites(server):
    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        client.handle(
            dict(
                type="start",
                independent=True,
                captureId="native-first",
                microphone=False,
                timelineOriginMs=int(time.time() * 1000),
                features=["source-outcomes-v1"],
            )
        )
        source = dict(sourceTrackId="never-opened", kind="remote", failed=True)
        assert client.handle(dict(type="sources", discoveredSources=[source])) == dict(type="ack")
        capture = service.captures["native-first"]
        capture.stop(reason="user")
        saved = json.loads((capture.directory / "session.json").read_text())
        assert saved["tracks"] == []
        assert saved["captureOutcome"]["sources"][0]["captureOutcome"] == "failed"
        final = (capture.directory / "session.json").read_bytes()
        assert client.handle(dict(type="stop", captureId="native-first"))["type"] == "stopped"
        late = client.handle(dict(type="sources", discoveredSources=[dict(source, sourceTrackId="too-late")]))
        assert late["scope"] == "command" and late["code"] == "state"
        assert (capture.directory / "session.json").read_bytes() == final
    finally:
        client.close()
