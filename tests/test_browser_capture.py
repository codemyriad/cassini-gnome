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

from cassini_viewer.browser_audio_proxy import BrowserAudioProxy
from cassini_viewer.browser_service import BrowserService, request


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
    service = BrowserService(tmp_path / "config.json", capture_parent=tmp_path / "library", tap_factory=Tap)
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    yield service, path, tmp_path / "library"
    service.closed.set()
    worker.join(2)
    assert not worker.is_alive()


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


def test_stop_before_missing_browser_fallback_begins_cannot_create_a_late_folder(server, monkeypatch):
    from test_browser_sessions import Extension

    from cassini_viewer.browser_capture import BrowserCapture

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

    from cassini_viewer.browser_service import send_packet

    peer.sessions[tab] = dict(recording=True, tabId=tab, captureId=capture.capture_id, path=str(capture.directory))
    send_packet(peer.stream, dict(type="status", recording=True, sessions=list(peer.sessions.values())))
    wait_for(
        lambda: any(
            s["id"].endswith(":" + str(tab)) and s.get("captureId") == capture.capture_id
            for s in request(path=peer.path)["statuses"]
        )
    )


def test_independent_call_hangup_stops_without_system_fallback(server):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    from cassini_viewer.browser_service import send_packet

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
            service._capture_missing_since["reload"] = time.monotonic() - 30.01
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
        service._capture_missing_since["disconnected"] = time.monotonic() - 30.01
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
