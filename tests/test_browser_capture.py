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
from broker_fixture import authorize_native_start as authorize_broker_start
from broker_fixture import stop_broker
from durable import DURABLE_BOUND, CaptureChanges, durable, saved, tracks_bound

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.browser_service import BrowserService, request

_SERVER_SERVICES = {}


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


def stream(capture, kind="microphone"):
    """Deliver the fake device's PCM once its side has committed.

    A tap's PCM before its side commits is readiness PCM and is discarded
    (R79 §1), as a running device keeps delivering after it opened.
    """
    identifier, tap = capture.native_taps[kind]
    track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
    tap.append(struct.pack("<hhhh", 100, 200, 300, 400), track["startOffsetMs"])
    return track


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
    # A fallback's commit and a stop's save write durably: tests wait on them as they commit.
    service.changes = CaptureChanges(service)
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    _SERVER_SERVICES[str(path)] = service
    try:
        yield service, path, tmp_path / "library"
    finally:
        try:
            stop_broker(service, worker, path)
        finally:
            _SERVER_SERVICES.pop(str(path))


def authorize_native_start(client=None, *, service=None, call_generation=None, microphone=True):
    service = service or _SERVER_SERVICES[client.connection.getpeername()]
    return authorize_broker_start(service, call_generation=call_generation, microphone=microphone)


def start(
    client,
    identifier,
    *,
    microphone=True,
    capture_local=None,
    call_generation=None,
    document_generation=None,
    request_id=None,
    service=None,
):
    if request_id is None:
        request_id = authorize_native_start(
            client, service=service, call_generation=call_generation, microphone=microphone
        )
    return client.handle(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId=identifier,
            timelineOriginMs=int(time.time() * 1000),
            microphone=microphone,
            title=identifier,
            callGeneration=call_generation,
            documentGeneration=document_generation,
            **({"captureLocal": capture_local} if capture_local is not None else {}),
            **({"requestId": request_id} if request_id is not None else {}),
        )
    )


def local(client, identifier, source_track_id="me"):
    client.handle(
        dict(
            type="track",
            id=identifier,
            label="Microphone",
            kind="local",
            sourceTrackId=source_track_id,
            sampleRate=48000,
            channels=1,
        )
    )
    client.handle(
        dict(type="audio", id=identifier, sequence=0, offsetMs=200, pcm=base64.b64encode(b"\x01\x00" * 4).decode())
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
                requestId=authorize_native_start(client),
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
        assert Tap.instances == []
        local(one, "local-one")
        local(two, "local-two")
        remote(one, "remote-one")
        remote(two, "remote-two")
        assert len([s for s in request(path=path)["statuses"] if s["recording"]]) == 2
        one.handle(dict(type="stop"))
        states = request(path=path)["statuses"]
        assert [s["captureId"] for s in states if s["recording"]] == ["second"]
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
        assert [t["kind"] for t in first["tracks"]] == ["local", "remote"]
        assert [t["kind"] for t in second["tracks"]] == ["remote"]
        assert first["tracks"][0]["labelSource"] == "browser"
        assert first["tracks"][0]["startOffsetMs"] == 200 and first["tracks"][0]["frames"] == 4
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


def test_audio_peer_eof_falls_back_only_its_own_capture_without_native_microphone(server):
    service, path, root = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        first = start(one, "first")
        start(two, "second", microphone=False)
        local(one, "local-one")
        remote(one, "remote-one")
        remote(two, "remote-two")
        one.close()
        service.changes.wait(lambda: service.captures["first"].browser_lost, "EOF fallback was not committed")
        assert [s["captureId"] for s in request(path=path)["statuses"] if s["recording"]] == ["first", "second"]
        from pathlib import Path

        value = json.loads((Path(first["path"]) / "session.json").read_text())
        assert value["state"] == "recording"
        assert value["events"][-1] == dict(
            atMs=value["events"][-1]["atMs"],
            type="othersSource",
            source="system",
            reason="lost",
            cause="transportClosed",
            detail="Browser disconnected before recording was stopped.",
        )
        assert [t["kind"] for t in value["tracks"]] == ["local", "remote", "system"]
        assert value["tracks"][0]["ended"] and value["tracks"][1]["ended"]
        assert value["tracks"][-1]["startOffsetMs"] == value["events"][-1]["atMs"]
        assert service.captures["first"].microphone is None
        assert set(service.captures["first"].native_taps) == {"system"}
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
        start(client, "one", capture_local=False)
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
            assert first.result(DURABLE_BOUND)["path"] == second.result(DURABLE_BOUND)["path"]
        assert capture.finished
    finally:
        client.close()


def test_rejected_browser_start_uses_its_own_system_fallback_without_forcing_microphone(server):
    from test_browser_service import wait_for
    from test_browser_sessions import Extension

    service, path, _ = server
    peer = Extension(path, (3,))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path, microphone=False)
            peer.answer(peer.receive(), ok=False)
            # Its system fallback starts a folder durably (7 fsyncs).
            result = pending.result(DURABLE_BOUND)
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
        # The side's span starts when it opened, within the 3 s bound.
        assert 0 <= value["tracks"][0]["startOffsetMs"] < 3000
        # The rejected fake browser never services a later Stop. Retire that
        # transport explicitly instead of racing its final status and waiting
        # for the production 25 s batch deadline. The native fallback survives.
        peer.close()
        wait_for(lambda: not service.browsers)
        assert not capture.finished
        request("stop-all", path=path)
        assert capture.finished
    finally:
        peer.close()


def test_rpc_selection_stops_only_its_sources_and_retains_browser_span_identity(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "selection")
        local(client, "me", source_track_id="me-source")
        remote(client, "remote")
        capture = service.captures["selection"]
        assert capture.microphone is None and Tap.instances == []
        selected = request("selection", path=path, directory=str(capture.directory), me=False, others=True)
        assert capture.microphone is None and Tap.instances == []
        first_me = next(t for t in capture.host.session.manifest["tracks"] if t["kind"] == "local")
        assert first_me["ended"]
        frozen_me = (capture.directory / first_me["file"]).read_bytes()
        client.handle(
            dict(
                type="audio",
                id="me",
                sequence=1,
                offsetMs=selected["events"][-1]["atMs"] + 10,
                pcm=base64.b64encode(b"\x02\x00" * 4).decode(),
            )
        )
        selected = request("selection", path=path, directory=str(capture.directory), me=True, others=False)
        assert capture.microphone is None and Tap.instances == []
        first_remote = next(t for t in capture.host.session.manifest["tracks"] if t["kind"] == "remote")
        assert first_remote["ended"]
        frozen = (capture.directory / first_remote["file"]).read_bytes()
        offset = selected["events"][-1]["atMs"] + 10
        client.handle(
            dict(type="audio", id="me", sequence=2, offsetMs=offset, pcm=base64.b64encode(b"\x03\x00" * 4).decode())
        )
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
        locals_ = [t for t in capture.host.session.manifest["tracks"] if t["kind"] == "local"]
        assert len(locals_) == 2 and locals_[0]["id"] != locals_[1]["id"]
        assert locals_[0]["sourceTrackId"] == locals_[1]["sourceTrackId"] == "me-source"
        assert (capture.directory / first_me["file"]).read_bytes() == frozen_me
        remotes = [t for t in capture.host.session.manifest["tracks"] if t["kind"] == "remote"]
        assert len(remotes) == 2 and remotes[0]["id"] != remotes[1]["id"]
        assert remotes[0]["sourceTrackId"] == remotes[1]["sourceTrackId"] == "remote"
        assert (capture.directory / first_remote["file"]).read_bytes() == frozen
        assert Tap.instances == []
        client.handle(dict(type="stop"))
    finally:
        client.close()


def ended_tracks(session, count):
    """Build the limit fixture in one real durable transaction, before the tested action."""
    with session.change([]):
        for index in range(count):
            identifier = f"ended-{index}"
            session.add_track(identifier, "Old", "remote", 48000, 1)
            session.end_track(identifier)
    committed = {track["id"]: track for track in json.loads((session.directory / "session.json").read_text())["tracks"]}
    for index in range(count):
        track = committed[f"ended-{index}"]
        assert track["ended"] and track["frames"] == 0
        with wave.open(str(session.directory / track["file"])) as wav:
            assert (wav.getnframes(), wav.getframerate(), wav.getnchannels()) == (0, 48000, 1)


def test_source_limit_failure_keeps_last_applied_choice_and_tracks(server):
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "limit", microphone=False, capture_local=False)
        remote(client, "remote")
        capture = service.captures["limit"]
        with capture.lock:
            ended_tracks(capture.host.session, 63)
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
            # Held while the test adds a 64th track: its checkpoint syncs every
            # open track's WAV (66 fsyncs) before release.set().
            durable(release, "the test did not release the native tap", tracks_bound(64))
            super().start()

    try:
        start(client, "rollback", microphone=False, capture_local=False)
        remote(client, "remote")
        capture = service.captures["rollback"]
        capture.tap_factory = ReadyTap
        session = capture.host.session
        if failure == "track-limit-race":
            with capture.lock:
                ended_tracks(session, 62)
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
    service.changes.wait(lambda: capture.browser_lost, "the capture did not fall back")
    wait_for(lambda: not service.browsers)
    request("stop-all", path=path)
    saved(capture)
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
        local(client, "me")
        remote(client, "remote")
        capture = service.captures["hangup"]
        bind_capture(peer, capture)
        send_packet(peer.stream, dict(type="meetings", meetings=[dict(meeting(), sourceCount=0)]))
        wait_for(lambda: "hangup" in service._capture_missing_since)
        assert not capture.browser_lost and not capture.finished
        assert Tap.instances == [] and capture.microphone is None
        with service.lock:
            service._capture_missing_since["hangup"] = time.monotonic() - 2.01
        request(path=path)  # Busy accept traffic must also advance policy.
        command = peer.receive()
        assert command["action"] == "stop"
        peer.answer(command)
        saved(capture)
        manifest = json.loads((capture.directory / "session.json").read_text())
        assert manifest["state"] == "completed"
        assert [t["kind"] for t in manifest["tracks"]] == ["local", "remote"]
        assert Tap.instances == []
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
        service.changes.wait(lambda: capture.browser_lost, "the capture did not fall back")
        assert not capture.finished and set(capture.native_taps) == {"system"} and capture.microphone is None
        peer.inventory((3,))
        wait_for(lambda: "reload" not in service._capture_missing_since)
        assert capture.browser_lost  # No automatic switch back.
        peer.inventory(())
        wait_for(lambda: "reload" in service._capture_missing_since)
        with service.lock:
            service._capture_missing_since["reload"] = time.monotonic() - 120.01
        service.policy()
        saved(capture)
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
    local(client, "me")
    remote(client, "remote")
    capture = service.captures["disconnected"]
    bind_capture(peer, capture)
    peer.close()
    wait_for(lambda: not service.browsers)
    client.close()
    service.changes.wait(lambda: capture.browser_lost, "the capture did not fall back")
    assert set(capture.native_taps) == {"system"} and capture.microphone is None
    stream(capture, "system")
    service.policy()
    assert not capture.finished
    with service.lock:
        service._capture_missing_since["disconnected"] = time.monotonic() - 120.01
    service.policy()
    saved(capture)
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
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "unobserved-loss")
        remote(client, "remote")
        capture = service.captures["unobserved-loss"]
        assert "unobserved-loss" not in service._capture_observed
        client.close()
        service.changes.wait(lambda: capture.browser_lost, "the capture did not fall back")
        service.policy()
        assert "unobserved-loss" in service._capture_missing_since
        assert service._capture_missing_since["unobserved-loss"] == capture.browser_lost_since
        assert not capture.finished
        with service.lock:
            service._capture_missing_since.pop("unobserved-loss")
            capture.browser_lost_since = time.monotonic() - 120.01
        service.policy()
        saved(capture)
        assert all(tap.stopped for tap in Tap.instances)
        assert json.loads((capture.directory / "session.json").read_bytes())["state"] == "completed"
    finally:
        client.close()


@pytest.mark.parametrize("reply_type", ["started", "ack", "error", "stopped"])
def test_lost_audio_acknowledgement_falls_back_to_system_without_native_mic(server, monkeypatch, reply_type):
    import socket

    from cassini_talk import browser_service

    service, _path, _root = server
    native, peer = socket.socketpair()
    native_stream, peer_stream = native.makefile("rwb"), peer.makefile("rwb")
    accepted, release, done = threading.Event(), threading.Event(), threading.Event()
    send = browser_service.send_packet
    failures = []

    def blocked_ack(stream, packet):
        if threading.current_thread() is actor and packet.get("type") == reply_type and not accepted.is_set():
            accepted.set()
            assert release.wait(3), "The peer-loss barrier was not released"
        send(stream, packet)

    def audio():
        try:
            service._audio(native_stream)
        except ConnectionError:
            pass  # A vanished peer cannot receive an error reply either.
        except Exception as error:
            failures.append(error)
        finally:
            try:
                native_stream.close()
            except ConnectionError:
                pass  # A buffered failed reply may fail again on close.
            finally:
                native.close()
                done.set()

    actor = threading.Thread(target=audio)
    monkeypatch.setattr(browser_service, "send_packet", blocked_ack)
    actor.start()
    try:
        assert browser_service.read_packet(peer_stream) == dict(ready=True, protocol=2, identity=service.identity)
        send(
            peer_stream,
            dict(
                requestId=authorize_native_start(service=service),
                type="start",
                independent=True,
                captureId="ack-loss",
                title="ack-loss",
                timelineOriginMs=int(time.time() * 1000),
            ),
        )
        if reply_type != "started":
            assert browser_service.read_packet(peer_stream)["type"] == "started"
            packet = (
                dict(type="track", id="remote", kind="remote", sampleRate=48000, channels=1)
                if reply_type == "ack"
                else dict(type="stop" if reply_type == "stopped" else "invalid-message")
            )
            send(peer_stream, packet)
        # The actor starts the capture (or adds its track) durably first.
        durable(accepted, "the real audio actor did not reach its acknowledgement")
        capture = service.captures["ack-loss"]
        assert capture.microphone is None
        # Retire the actual peer after acceptance but before its reply. This
        # forces EPIPE in flush(), not the orderly EOF path already covered.
        peer.shutdown(socket.SHUT_RDWR)
        peer_stream.close()
        peer.close()
        release.set()
        # It falls back, or stops, durably before it ends.
        durable(done, "the vanished audio peer stranded the capture actor")
        assert not failures
        if reply_type in ("error", "stopped"):
            assert capture.finished and not capture.browser_lost
            assert capture.host.session.manifest["events"][-1]["reason"] == (
                "error" if reply_type == "error" else "user"
            )
            return
        assert not capture.finished and capture.browser_lost
        assert capture.microphone is None and set(capture.native_taps) == {"system"}
        assert capture.host.session.manifest["state"] == "recording"
        assert capture.host.session.manifest["events"][-1]["reason"] == "lost"
        system_id, system = capture.native_taps["system"]
        system_track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == system_id)
        system.append(b"\x01\x00" * 480, system_track["startOffsetMs"] + 100)
        assert not capture.host.session.manifest.get("problemKind")
        capture.stop(reason="user")
        assert capture.host.session.manifest["events"][-1]["reason"] == "user"
    finally:
        release.set()
        peer_stream.close()
        peer.close()
        actor.join(5)
        assert not actor.is_alive(), "The audio actor did not exit after peer loss"


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
            stream(capture, "system")
            peer.close()
            wait_for(lambda: not service.browsers)
        finally:
            peer.close()
    key = capture.capture_id
    assert key not in service._capture_observed and capture.browser_lost
    with service.lock:
        capture.browser_lost_since = time.monotonic() - 120.01
    service.policy()
    saved(capture)
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
        service.changes.wait(lambda: capture.browser_lost, "the capture did not fall back")
        assert not capture.finished and set(capture.native_taps) == {"system"} and capture.microphone is None
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
    client, browser_me = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        start(browser_me, "browser-me-permissions", microphone=True)
        local(browser_me, "local-me")
        remote(browser_me, "remote-browser")
        assert service.captures["browser-me-permissions"].snapshot()["permittedSources"] == []
        service.captures["browser-me-permissions"].fallback()
        identifier, system = service.captures["browser-me-permissions"].native_taps["system"]
        track = next(
            t
            for t in service.captures["browser-me-permissions"].host.session.manifest["tracks"]
            if t["id"] == identifier
        )
        system.append(b"\1\0" * 4, track["startOffsetMs"] + 100)
        assert service.captures["browser-me-permissions"].snapshot()["permittedSources"] == ["system"]
        service.captures["browser-me-permissions"].stop()

        start(client, "permissions", microphone=False, capture_local=False)
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
        browser_me.close()
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
            saved(capture)
            wait_for(lambda: ("4242:99", 3) in service.stopped_calls)
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
        # This peer is alive: answer its real Stop instead of accidentally
        # consuming the broker's 25 s dead-peer deadline during cleanup.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(1) as pool:
            stopped = pool.submit(request, "stop-all", path=path)
            command = peer.receive()
            assert command["action"] == "stop" and command["tabId"] == 3
            peer.answer(command)
            stopped.result(2)
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
                requestId=authorize_native_start(client),
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


def test_initial_system_stand_in_retains_the_selected_logical_call_generation(server):
    from test_browser_service import meeting, wait_for
    from test_browser_sessions import Extension

    from cassini_talk.browser_service import send_packet

    service, path, _root = server
    peer = Extension(path, (3,), instance="stand-in-browser")
    try:
        send_packet(
            peer.stream, dict(type="hello", version=1, capabilities=["independent-sessions-v1", "multi-stream-v1"])
        )
        send_packet(
            peer.stream, dict(type="meetings", meetings=[dict(meeting(), callGeneration="call-before-browser-audio")])
        )
        wait_for(
            lambda: (
                next(iter(service.browsers.values())).meetings[3].get("callGeneration") == "call-before-browser-audio"
            )
        )
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path, microphone=False)
            command = peer.receive()
            assert command["callGeneration"] == "call-before-browser-audio"
            peer.answer(command, ok=False)
            # Its system stand-in starts a folder durably (6 fsyncs).
            result = pending.result(DURABLE_BOUND)
        assert not result["failures"]
        (capture,) = service.captures.values()
        assert capture.browser_lost and not capture.audio_connected
        assert capture.call_generation == "call-before-browser-audio"
        assert capture.microphone is None
        capture.stop()
    finally:
        peer.close()


def test_native_mic_checkpoint_failure_keeps_browser_pcm_and_does_not_raise_global_capture_error(server, monkeypatch):
    import errno

    from test_capture_record import declare, pcm

    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        client.handle(
            dict(
                requestId=authorize_native_start(client),
                type="start",
                version=1,
                independent=True,
                captureId="mic-sync",
                timelineOriginMs=int(time.time() * 1000),
                microphone=True,
                captureLocal=False,
                features=["source-outcomes-v1"],
            )
        )
        capture = service.captures["mic-sync"]
        tap = capture.microphone
        session = capture.host.session
        identifier = capture.native_taps["microphone"][0]
        tap.append(b"\x01\x00" * 48000, 200)
        writer = session.writers[identifier]

        def broken():
            if not writer.file.closed:
                raise OSError(errno.ENOSPC, "Generated mic fsync failure")

        monkeypatch.setattr(writer, "sync", broken)
        assert client.handle(declare("A")) == dict(type="ack")
        assert client.handle(pcm("A", 48000, offset=2000)) == dict(type="ack")
        before = next(t["frames"] for t in session.manifest["tracks"] if t["id"] == identifier)
        tap.append(b"\x02\x00" * 48000, 3000)
        assert next(t["frames"] for t in session.manifest["tracks"] if t["id"] == identifier) == before
        assert not capture.finished and capture.error is None
        assert client.handle(pcm("A", 48000, sequence=1, offset=3000)) == dict(type="ack")
        client.handle(dict(type="stop", captureId="mic-sync"))
        assert capture.finished and session.manifest["problemKind"] == "io"
    finally:
        client.close()


def test_announced_page_microphone_never_interrupts_a_native_me_recording(server):
    from test_capture_record import declare, pcm

    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        reply = client.handle(
            dict(
                requestId=authorize_native_start(client),
                type="start",
                independent=True,
                captureId="native-me",
                microphone=True,
                captureLocal=False,
                timelineOriginMs=int(time.time() * 1000),
                features=["source-outcomes-v1"],
            )
        )
        assert reply["features"] == ["source-outcomes-v1"]
        sources = [
            dict(sourceTrackId="page-mic", kind="local", label="Microphone"),
            dict(sourceTrackId="guest", kind="remote", label="Guest"),
        ]
        assert client.handle(dict(type="sources", discoveredSources=sources)) == dict(type="ack")
        assert client.handle(declare("guest-span", "guest")) == dict(type="ack")
        assert client.handle(pcm("guest-span", 48000)) == dict(type="ack")
        capture = service.captures["native-me"]
        assert client.handle(dict(type="stop", captureId="native-me"))["type"] == "stopped"
        saved = json.loads((capture.directory / "session.json").read_text())
        rows = {row["sourceTrackId"]: row for row in saved["captureOutcome"]["sources"]}
        assert rows["page-mic"]["captureExpected"] is False and rows["page-mic"]["captureOutcome"] == "neverAudio"
        assert any(t["kind"] == "microphone" for t in saved["tracks"]), "Me is the native microphone copy"
        assert saved["state"] == "completed" and "problemKind" not in saved
    finally:
        client.close()


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("audio_first", [False, True])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_snapshot_keeps_host_start_origin_after_toggle_and_control_disconnect(
    server, automatic, audio_first, acknowledged
):
    from test_browser_service import browser, wait_for

    from cassini_talk.browser_service import read_packet, send_packet

    service, path, _root = server
    connection, control = browser(path, "origin-test")
    client = BrowserAudioProxy(path=path)
    try:
        with ThreadPoolExecutor(1) as pool:
            if automatic:
                request("configure", path=path, autoRecord=True)
                future = None
            else:
                identifier = request(path=path)["meetings"][0]["id"]
                future = pool.submit(request, "start", path=path, id=identifier)
            command = read_packet(control)
            assert command["action"] == "start"
            if audio_first:
                start(
                    client,
                    "origin",
                    call_generation="call-fixture",
                    document_generation="hook-fixture",
                    request_id=command["requestId"],
                )
            status = dict(recording=True, tabId=3, captureId="origin", automatic=not automatic)
            if acknowledged:
                send_packet(control, dict(type="result", requestId=command["requestId"], ok=True, status=status))
                if future:
                    future.result(2)
            else:
                send_packet(control, dict(type="status", **status))
            if not audio_first:
                start(
                    client,
                    "origin",
                    call_generation="call-fixture",
                    document_generation="hook-fixture",
                    request_id=command["requestId"],
                )
            wait_for(lambda: service._capture_automatic.get("origin") is automatic)
            request("configure", path=path, autoRecord=not automatic)
            assert (
                next(s for s in request(path=path)["statuses"] if s.get("captureId") == "origin")["automatic"]
                is automatic
            )
            if not acknowledged:
                # Release the held response only after proving origin is
                # available without it (including after the toggle).
                send_packet(control, dict(type="result", requestId=command["requestId"], ok=True, status=status))
                if future:
                    future.result(2)
            control.close()
            connection.close()
        wait_for(lambda: not service.browsers)
        assert (
            next(s for s in service.snapshot()["statuses"] if s.get("captureId") == "origin")["automatic"] is automatic
        )
        client.handle(dict(type="stop", captureId="origin"))
    finally:
        client.close()
        control.close()
        connection.close()


@pytest.fixture
def pending_automatic_reconnect(server, monkeypatch):
    """A real automatic Start loses control before status/ack; hold recovery
    until the test has inspected binding on the replacement connection (E1)."""
    from types import SimpleNamespace

    from test_browser_service import browser

    from cassini_talk.browser_service import send_packet

    service, path, _root = server
    entered, release, completed, reported = (threading.Event() for _ in range(4))
    recovered, remembered, errors = [], [], []
    original_recover, original_command = service._recover_command, service.command
    original_update, original_remember = service.update, service._remember_capture_origin

    def recover(*args):
        entered.set()
        durable(release, "test did not release Start recovery")
        result = original_recover(*args)
        recovered.append(result)
        return result

    def command(*args, **kwargs):
        try:
            return original_command(*args, **kwargs)
        except BaseException as error:
            errors.append(error)
            raise
        finally:
            completed.set()

    def update(peer, packet):
        original_update(peer, packet)
        if packet.get("type") == "status" and packet.get("captureId") == "recovered-origin":
            reported.set()

    def remember(pending, target, packet):
        remembered.append((pending["automatic"], target, packet, service.lock._is_owned()))
        return original_remember(pending, target, packet)

    monkeypatch.setattr(service, "_recover_command", recover)
    monkeypatch.setattr(service, "command", command)
    monkeypatch.setattr(service, "update", update)
    monkeypatch.setattr(service, "_remember_capture_origin", remember)
    connection, control = browser(path, "pending-origin")
    replacement = replacement_control = client = capture = None
    try:
        from cassini_talk.browser_service import read_packet

        request("configure", path=path, autoRecord=True)
        asked = read_packet(control)
        assert asked["action"] == "start"
        with service.lock:
            original_peer = next(iter(service.browsers.values()))
            logical = service._meeting_id(original_peer, 3)
            pending = original_peer.pending[asked["requestId"]]
            assert pending["automatic"] and pending["result"] is None
        client = BrowserAudioProxy(path=path)
        start(
            client,
            "recovered-origin",
            call_generation="call-fixture",
            document_generation="hook-fixture",
            request_id=asked["requestId"],
        )
        remote(client, "voice")
        capture = service.captures["recovered-origin"]
        # No status or result is sent on this socket. Its pending command
        # survives only on the retired peer and in _pending_calls[logical].
        control.close()
        connection.close()
        durable(entered, "automatic Start did not enter reconnect recovery")
        replacement, replacement_control = browser(path, "pending-origin")
        send_packet(
            replacement_control,
            dict(type="status", recording=True, tabId=3, captureId="recovered-origin", automatic=False),
        )
        durable(reported, "replacement recording status was not processed")
        with service.lock:
            peer = next(iter(service.browsers.values()))
            assert not peer.pending and pending is service._pending_calls[logical][asked["requestId"]]
        yield SimpleNamespace(
            service=service,
            path=path,
            client=client,
            capture=capture,
            pending=pending,
            logical=logical,
            release=release,
            completed=completed,
            recovered=recovered,
            remembered=remembered,
            errors=errors,
        )
    finally:
        release.set()
        if entered.is_set():
            durable(completed, "recovered automatic Start did not finish")
        if capture is not None:
            capture.stop()
        if client is not None:
            client.close()
        if replacement_control is not None:
            replacement_control.close()
            replacement.close()
        control.close()
        connection.close()


def test_automatic_origin_binds_before_and_is_remembered_after_start_recovery(pending_automatic_reconnect):
    case = pending_automatic_reconnect
    assert not case.completed.is_set(), "Start recovery must remain held while binding is checked"
    status = next(s for s in case.service.snapshot()["statuses"] if s.get("captureId") == "recovered-origin")
    assert status["automatic"] is True, "replacement must bind origin from the logical pending call"
    case.release.set()
    durable(case.completed, "automatic Start recovery did not finish")
    assert not case.errors and len(case.recovered) == 1 and case.recovered[0]["ok"]
    assert case.pending["result"] is None, "no original Start acknowledgement was delivered"
    assert len(case.remembered) == 1, "successful command recovery must remember origin explicitly"
    automatic, target, packet, under_lock = case.remembered[0]
    assert automatic and under_lock and target["captureId"] == "recovered-origin" and packet["ok"]
    assert case.logical not in case.service._pending_calls
    status = next(s for s in case.service.snapshot()["statuses"] if s.get("captureId") == "recovered-origin")
    assert status["automatic"] is True


@pytest.mark.parametrize("audio_first", [False, True])
def test_automatic_status_without_browser_instance_binds_origin_before_start_ack(server, monkeypatch, audio_first):
    from test_browser_service import browser

    from cassini_talk.browser_service import read_packet, send_packet

    service, path, _root = server
    reported = threading.Event()
    original_update = service.update

    def update(peer, packet):
        original_update(peer, packet)
        if packet.get("type") == "status" and packet.get("captureId") == "unidentified-origin":
            reported.set()

    monkeypatch.setattr(service, "update", update)
    connection, control = browser(path, instance=None)
    client = BrowserAudioProxy(path=path)
    capture = None
    try:
        request("configure", path=path, autoRecord=True)
        asked = read_packet(control)
        assert asked["action"] == "start"
        if audio_first:
            start(
                client,
                "unidentified-origin",
                call_generation="call-fixture",
                document_generation="hook-fixture",
                request_id=asked["requestId"],
            )
            capture = service.captures["unidentified-origin"]
        send_packet(control, dict(type="status", recording=True, tabId=3, captureId="unidentified-origin"))
        durable(reported, "unidentified peer's recording status was not processed")
        with service.lock:
            peer = next(iter(service.browsers.values()))
            assert peer.browser_instance is None and peer.pending[asked["requestId"]]["result"] is None
            assert service._capture_automatic.get("unidentified-origin") is True
        if not audio_first:
            start(
                client,
                "unidentified-origin",
                call_generation="call-fixture",
                document_generation="hook-fixture",
                request_id=asked["requestId"],
            )
            capture = service.captures["unidentified-origin"]
        status = next(s for s in service.snapshot()["statuses"] if s.get("captureId") == "unidentified-origin")
        assert status["automatic"] is True
        # Retire the request only after checking status-before-ack behavior.
        send_packet(
            control,
            dict(
                type="result",
                requestId=asked["requestId"],
                ok=True,
                status=dict(recording=True, tabId=3, captureId="unidentified-origin"),
            ),
        )
    finally:
        control.close()
        connection.close()
        if capture is not None:
            capture.stop()
        client.close()


def test_browser_local_me_capture_meters_levels_and_accepts_reload_gap_without_native_mic(server):
    import math

    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        client.handle(
            dict(
                requestId=authorize_native_start(client),
                type="start",
                version=1,
                independent=True,
                captureId="option-a",
                timelineOriginMs=int(time.time() * 1000),
                microphone=True,
                title="option-a",
                features=["source-outcomes-v1"],
            )
        )
        capture = service.captures["option-a"]
        assert not capture.native_me and capture.microphone is None and Tap.instances == []

        tone_100ms = b"".join(
            struct.pack("<h", round(12000 * math.sin(2 * math.pi * 660 * index / 48000))) for index in range(4800)
        )
        encoded = base64.b64encode(tone_100ms).decode()
        client.handle(
            dict(
                type="track",
                id="me-1",
                label="Headset mic",
                kind="local",
                sourceTrackId="doc-1:source:0",
                sampleRate=48000,
                channels=1,
            )
        )
        client.handle(dict(type="audio", id="me-1", sequence=0, offsetMs=0, pcm=encoded))
        assert capture.levels[0] > 0.0 and capture.levels[1] == 0.0

        capture.select(False, True)
        assert capture.levels[0] == 0.0 and Tap.instances == []
        assert next(t for t in capture.host.session.manifest["tracks"] if t["id"] == "me-1")["ended"]

        capture.select(True, True)
        reopen_at = capture.host.session.manifest["events"][-1]["atMs"] + 10
        client.handle(dict(type="audio", id="me-1", sequence=1, offsetMs=reopen_at, pcm=encoded))
        assert capture.levels[0] > 0.0 and Tap.instances == []

        client.handle(dict(type="trackEnd", id="me-1", reason="ended", droppedFrames=0, endOffsetMs=reopen_at + 100))
        assert capture.levels[0] == 0.0

        client.handle(
            dict(
                type="track",
                id="me-2",
                label="Headset mic",
                kind="local",
                sourceTrackId="doc-1:source:0",
                sampleRate=48000,
                channels=1,
            )
        )
        client.handle(dict(type="audio", id="me-2", sequence=0, offsetMs=reopen_at + 200, pcm=encoded))
        remote(client, "remote-1")
        assert capture.levels[0] > 0.0

        # Page reload / transport loss: Me accepts a gap and NEVER falls back to the raw OS microphone.
        capture.fallback()
        assert capture.browser_lost
        assert capture.levels[0] == 0.0
        assert capture.microphone is None and set(capture.native_taps) == {"system"}
        assert next(t for t in capture.host.session.manifest["tracks"] if t["id"] == "me-2")["ended"]

        # Browser returns after reload: both local Me and remote Others resume on the wire.
        restore_at = capture.host.session.manifest["events"][-1]["atMs"] + 200
        client.handle(
            dict(
                type="track",
                id="me-3",
                label="Headset mic",
                kind="local",
                sourceTrackId="doc-2:source:0",
                sampleRate=48000,
                channels=1,
            )
        )
        client.handle(
            dict(
                type="track",
                id="remote-2",
                label="Guest",
                kind="remote",
                sourceTrackId="doc-2:source:1",
                sampleRate=48000,
                channels=1,
            )
        )
        client.handle(dict(type="audio", id="me-3", sequence=0, offsetMs=restore_at, pcm=encoded))
        client.handle(dict(type="audio", id="remote-2", sequence=0, offsetMs=restore_at, pcm=encoded))
        capture.restore()
        assert not capture.browser_lost and not capture.native_taps and capture.microphone is None
        client.handle(dict(type="audio", id="me-3", sequence=1, offsetMs=restore_at + 100, pcm=encoded))
        assert capture.levels[0] > 0.0

        client.handle(dict(type="stop", captureId="option-a"))
        manifest = json.loads((capture.directory / "session.json").read_text())
        assert not any(t["kind"] == "microphone" for t in manifest["tracks"])
        assert [t["kind"] for t in manifest["tracks"]].count("local") == 4
    finally:
        client.close()


def test_start_fallback_stand_in_never_opens_native_microphone_while_signal_owned_does(server, tmp_path):
    from test_browser_service import meeting
    from test_browser_sessions import Extension

    from cassini_talk.browser_capture import BrowserCapture

    service, path, _root = server
    peer = Extension(path, (3,))
    signal_capture = None
    try:
        identifier = request(path=path)["meetings"][0]["id"]
        snapshot = service.start_fallback(identifier, meeting(), microphone=True)
        stand_in = service.captures[snapshot["captureId"]]
        assert not stand_in.native_me
        assert stand_in.microphone is None and set(stand_in.native_taps) == {"system"}
        manifest = json.loads((stand_in.directory / "session.json").read_text())
        assert [t["kind"] for t in manifest["tracks"]] == ["system"]
        assert dict(atMs=0, type="selection", me=True, others=True, reason="start") in manifest["events"]
        stand_in.stop()

        # D74 mixed-site signal-owned captures have no separated WebRTC capture and keep native microphone + system.
        signal_capture = BrowserCapture(lambda *_: None, parent=tmp_path / "signal-lib", tap_factory=Tap)
        signal_capture.start(
            dict(
                type="start",
                captureId="signal-owned",
                timelineOriginMs=int(time.time() * 1000),
                microphone=True,
            ),
            others_source="system",
            reason="start",
            others_route=dict(source="system", reason="unsupported"),
            native_me=True,
        )
        assert signal_capture.native_me
        assert set(signal_capture.native_taps) == {"microphone", "system"}
        assert signal_capture.microphone is not None
    finally:
        if signal_capture is not None:
            signal_capture.stop()
        peer.close()
