# SPDX-License-Identifier: GPL-3.0-or-later
"""D52.3c resume-capture-v1, host side: a native resume on the real broker and capture."""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from broker_fixture import stop_broker
from durable import CaptureChanges, tracks_bound
from test_browser_capture import Tap
from test_browser_liveness import Clock, Settled, me_frames
from test_browser_service import wait_for
from test_capture_record import declare, pcm

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.browser_capture import BrowserCapture
from cassini_talk.browser_service import AudioTransport, BrowserService, connect, read_packet, send_packet

ACK = dict(type="ack")
ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / "browser-protocol/conformance/control/resume-capture-vectors.json").read_text())
CONTROL = ["independent-sessions-v1", "call-evidence-v1", "page-state-v1", "multi-stream-v1", "resume-capture-v1"]


@pytest.fixture
def broker(tmp_path):
    Tap.instances = []
    clock = Clock()
    service = BrowserService(
        tmp_path / "config.json",
        capture_parent=tmp_path / "library",
        tap_factory=Tap,
        notifier=lambda _title, _stop: None,
        clock=clock,
    )
    # Capture changes as they commit; waits are on them, never a sleep.
    changes = CaptureChanges(service)
    path = tmp_path / "broker/browser.sock"
    ready = threading.Event()
    worker = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    yield SimpleNamespace(service=service, path=path, clock=clock, changes=changes, library=tmp_path / "library")
    stop_broker(service, worker, path)


def proxy(broker):
    return Settled(BrowserAudioProxy(path=broker.path), broker.service)


def started(broker, identifier="F", *, generation="call-A", document="doc-1"):
    client = proxy(broker)
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
            callGeneration=generation,
            documentGeneration=document,
        )
    )
    assert reply["type"] == "started"
    capture = broker.service.captures[identifier]
    capture.multistream_capable = True  # A multi-stream-v1 control peer sets this from policy().
    return client, capture


def lose(broker, client, capture):
    """The audio transport closes (EOF); the capture falls back to system/lost."""
    client.close()
    # The fallback ends every track durably: bounded against their count.
    tracks = len(capture.host.session.manifest["tracks"])
    broker.changes.wait(lambda: capture.browser_lost, "the capture did not fall back", tracks_bound(tracks))
    wait_for(lambda: not broker.service._audio_transports)


def resume(capture, **changes):
    packet = dict(
        type="resume",
        version=1,
        independent=True,
        captureId=capture.capture_id,
        timelineOriginMs=capture.origin_ms,
        callGeneration=capture.call_generation,
        features=["source-outcomes-v1", "resume-capture-v1"],
        title="A fresh Start's title",
        documentGeneration="doc-1",
    )
    packet.update(changes)
    return {key: value for key, value in packet.items() if value is not None}


def proof(capture, *features):
    return dict(
        type="started",
        path=str(capture.directory),
        features=[*features, "resume-capture-v1"],
        resumed=True,
        captureId=capture.capture_id,
        timelineOriginMs=capture.origin_ms,
        callGeneration=capture.call_generation,
    )


def sources(capture):
    return [(e["source"], e["reason"]) for e in capture.host.session.manifest["events"] if e["type"] == "othersSource"]


def folders(broker):
    return sorted(broker.library.iterdir()) if broker.library.exists() else []


def now_ms(capture):
    return int(capture.elapsed_ms())


def restored(broker, client, capture, identifier, root):
    """Fresh PCM on the resumed transport, then the ordinary restore commit."""
    assert client.handle(declare(identifier, root)) == ACK
    assert client.handle(pcm(identifier, 480, offset=now_ms(capture))) == ACK
    capture.restore()
    assert not capture.browser_lost
    # Recorded from the seam on: PCM after the commit point reaches its WAV.
    assert client.handle(pcm(identifier, 480, sequence=1, offset=now_ms(capture) + 1000)) == ACK


def test_native_resume_attaches_with_exact_proof_and_restores_on_the_same_folder(broker):
    client, capture = started(broker)
    folder, origin = capture.directory, capture.origin_ms
    microphone = capture.microphone
    assert client.handle(declare("guest", "doc-1:source:1")) == ACK
    assert client.handle(pcm("guest", 480)) == ACK
    lose(broker, client, capture)
    microphone.append(b"\x01\x00" * 480, 200)
    before = me_frames(capture)
    resumed = proxy(broker)
    try:
        answer = resumed.handle(resume(capture))
        assert answer == proof(capture, "source-outcomes-v1")
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads((ROOT / "browser-protocol/schemas/response.schema.json").read_text())
        jsonschema.validate(answer, schema)
        # Attached, but not switched: only PCM on the resumed transport and
        # the restore commit change the source.
        assert capture.audio_connected and capture.browser_lost
        assert sources(capture) == [("browser", "start"), ("system", "lost")]
        restored(broker, resumed, capture, "guest-again", "doc-1:source:2")
        assert sources(capture) == [("browser", "start"), ("system", "lost"), ("browser", "restored")]
        assert folders(broker) == [folder] and capture.directory == folder and capture.origin_ms == origin
        assert capture.microphone is microphone
        # At the capture's own clock: PCM stamped before the microphone side
        # committed is readiness PCM and is dropped, and that commit comes
        # later than 300 ms when the folder's durable writes are slow.
        microphone.append(b"\x02\x00" * 480, now_ms(capture))
        assert me_frames(capture) > before
        assert capture.host.session.manifest["title"] == "F", "a fresh Start's title does not modify the target"
        capture.stop()
    finally:
        resumed.close()


def test_a_resume_on_the_same_document_redeclares_its_sources(broker):
    """The page keeps one sourceTrackId per MediaStreamTrack root for the whole
    document, so the resumed transport re-declares the same roots under fresh
    span ids. The closed transport's spans are retired, not failed."""
    client, capture = started(broker)
    assert client.handle(declare("span-x", "doc-1:source:1")) == ACK
    assert client.handle(pcm("span-x", 480)) == ACK
    lose(broker, client, capture)
    ledger = capture.host.session.capture_record
    assert ledger.seen["doc-1:source:1"]["failed"] is False
    resumed = proxy(broker)
    try:
        assert resumed.handle(resume(capture))["resumed"] is True
        restored(broker, resumed, capture, "span-y", "doc-1:source:1")
        assert ledger.seen["doc-1:source:1"]["failed"] is False
        assert sources(capture)[-1] == ("browser", "restored")
        spans = [t for t in capture.host.session.manifest["tracks"] if t.get("sourceTrackId") == "doc-1:source:1"]
        assert len(spans) == 2 and spans[0]["ended"] and not spans[1].get("ended")
        # The resumed span's PCM after the seam reached its own WAV.
        assert ledger.received[spans[1]["id"]] == 480
        audio = (capture.directory / spans[1]["file"]).read_bytes()[44:]
        assert len(audio) == spans[1]["frames"] * 2 > 0
        manifest = capture.stop()
        assert manifest["type"] == "stopped"
        saved = json.loads((capture.directory / "session.json").read_text())
        outcome = {s["sourceTrackId"]: s["captureOutcome"] for s in saved["captureOutcome"]["sources"]}
        assert outcome["doc-1:source:1"] != "failed"
    finally:
        resumed.close()


def refusal(reply):
    assert reply["type"] == "error" and reply["code"] == "state" and reply["scope"] == "command", reply
    assert isinstance(reply["message"], str) and reply["message"]


def stopped(broker, capture):
    capture.stop()


def requested(broker, capture):
    capture.stop_requested.set()


def deselected(broker, capture):
    capture.select(True, False)


REFUSALS = dict(
    unknown=dict(packet=dict(captureId="not-a-capture")),
    origin=dict(packet=lambda capture: dict(timelineOriginMs=capture.origin_ms + 1)),
    float_origin=dict(packet=lambda capture: dict(timelineOriginMs=float(capture.origin_ms))),
    other_call=dict(packet=dict(callGeneration="call-B")),
    no_feature=dict(packet=dict(features=["source-outcomes-v1"])),
    not_a_list=dict(packet=dict(features="resume-capture-v1")),
    duplicate_feature=dict(packet=dict(features=["resume-capture-v1", "resume-capture-v1"])),
    version=dict(packet=dict(version=2)),
    dependent=dict(packet=dict(independent=None)),
    long_identity=dict(packet=dict(captureId="é" * 65)),
    stopped=dict(before=stopped),
    stop_requested=dict(before=requested),
    none_user=dict(before=deselected),
)


@pytest.mark.parametrize("name", REFUSALS)
def test_native_resume_refusals_have_no_side_effect(broker, name):
    client, capture = started(broker)
    assert client.handle(declare("guest", "doc-1:source:1")) == ACK
    assert client.handle(pcm("guest", 480)) == ACK
    lose(broker, client, capture)
    before = REFUSALS[name].get("before")
    if before is not None:
        before(broker, capture)
    packet = REFUSALS[name].get("packet", {})
    packet = packet(capture) if callable(packet) else packet
    events = json.dumps(capture.host.session.manifest["events"])
    state = (capture.finished, capture.browser_lost, capture.audio_connected, capture.directory)
    library = folders(broker)
    attempt = proxy(broker)
    try:
        refusal(attempt.handle(resume(capture, **packet)))
        assert folders(broker) == library
        # Closing the refused attempt must not stop or close the target.
        attempt.close()
        wait_for(lambda: not broker.service._audio_transports)
        assert json.dumps(capture.host.session.manifest["events"]) == events
        assert (capture.finished, capture.browser_lost, capture.audio_connected, capture.directory) == state
        if before is None:
            # The target still takes a correct resume afterwards.
            again = proxy(broker)
            try:
                assert again.handle(resume(capture))["resumed"] is True
            finally:
                again.close()
    finally:
        attempt.close()
        capture.stop()


@pytest.mark.parametrize("attached", ["live-browser", "control-loss-fallback"])
def test_a_resume_never_attaches_a_second_transport(broker, attached):
    client, capture = started(broker)
    try:
        assert client.handle(declare("guest", "doc-1:source:1")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        if attached == "control-loss-fallback":
            # Control loss commits system/lost while the original transport,
            # and its audio_connected, stay attached: the source alone would
            # look eligible.
            capture.fallback()
            assert sources(capture)[-1] == ("system", "lost") and capture.audio_connected
        events = json.dumps(capture.host.session.manifest["events"])
        attempt = proxy(broker)
        try:
            refusal(attempt.handle(resume(capture)))
        finally:
            attempt.close()
        wait_for(lambda: len(broker.service._audio_transports) == 1)
        assert json.dumps(capture.host.session.manifest["events"]) == events
        # The original transport is untouched and still delivers.
        assert client.handle(pcm("guest", 480, sequence=1, offset=10)) == ACK
        (transport,) = broker.service._audio_transports
        assert transport.capture is capture and not transport.shut_down.is_set()
    finally:
        client.close()
        capture.stop()


def test_concurrent_resumes_attach_exactly_one_transport(broker):
    client, capture = started(broker)
    lose(broker, client, capture)
    attempts = [proxy(broker), proxy(broker)]
    replies = [None, None]

    def ask(index):
        replies[index] = attempts[index].client.handle(resume(capture))

    try:
        with capture.transition:
            threads = [threading.Thread(target=ask, args=(index,), daemon=True) for index in (0, 1)]
            for thread in threads:
                thread.start()
            # Both resumes are inside the broker, waiting on the capture's
            # transition, before either may run.
            wait_for(lambda: sum(t.handling for t in broker.service._audio_transports) == 2)
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        accepted = [reply for reply in replies if reply.get("resumed") is True]
        refused = [reply for reply in replies if reply.get("type") == "error"]
        assert len(accepted) == 1 and len(refused) == 1
        refusal(refused[0])
        wait_for(lambda: len(broker.service._audio_transports) == 1)
        (transport,) = broker.service._audio_transports
        assert transport.capture is capture and capture.audio_connected
    finally:
        for attempt in attempts:
            attempt.close()
        capture.stop()


def test_an_attach_that_fails_after_acceptance_leaves_no_ghost_attachment(broker):
    client, capture = started(broker)
    lose(broker, client, capture)

    class Unowned(AudioTransport):
        def __setattr__(self, name, value):
            if name == "capture" and value is not None:
                raise RuntimeError("the transport could not take ownership")
            super().__setattr__(name, value)

    with pytest.raises(RuntimeError):
        broker.service._resume_attach(resume(capture), Unowned(None, broker.clock))
    # Not attached to anything: the next resume is still accepted.
    assert not capture.audio_connected and capture.browser_lost and not capture.finished
    again = proxy(broker)
    try:
        assert again.handle(resume(capture))["resumed"] is True
    finally:
        again.close()
        capture.stop()


def stand_in(broker, source, reason):
    capture = BrowserCapture(broker.service._capture_changed, parent=broker.library, tap_factory=Tap)
    capture.start(
        dict(type="start", captureId="F", timelineOriginMs=int(time.time() * 1000), callGeneration="call-A"),
        others_source=source,
        reason=reason,
    )
    capture.multistream_capable = True
    with broker.service.lock:
        broker.service.captures["F"] = capture
    return capture


@pytest.mark.parametrize("target", VECTORS["targets"]["eligible"], ids=lambda t: f"{t['source']}-{t['reason']}")
def test_shared_eligible_targets_resume_with_their_seam_reason(broker, target):
    if target["reason"] == "missing":
        capture = stand_in(broker, "system", "missing")  # D49
        features = ()
    else:
        client, capture = started(broker)
        lose(broker, client, capture)
        features = ("source-outcomes-v1",)
    assert sources(capture)[-1] == (target["source"], target["reason"])
    resumed = proxy(broker)
    try:
        assert resumed.handle(resume(capture)) == proof(capture, *features)
        restored(broker, resumed, capture, "returned", "doc-1:source:9")
        assert sources(capture)[-1] == ("browser", target["seamReason"])
    finally:
        resumed.close()
        capture.stop()


@pytest.mark.parametrize("target", VECTORS["targets"]["ineligible"], ids=lambda t: f"{t['source']}-{t['reason']}")
def test_shared_ineligible_targets_refuse_without_side_effects(broker, target):
    client, capture = started(broker)
    current = client
    try:
        if target["reason"] == "restored":
            lose(broker, client, capture)
            current = proxy(broker)
            assert current.handle(resume(capture))["resumed"] is True
            restored(broker, current, capture, "returned", "doc-1:source:9")
        elif target["source"] == "none":
            capture.select(True, False)
        assert sources(capture)[-1] == (target["source"], target["reason"])
        events = json.dumps(capture.host.session.manifest["events"])
        attempt = proxy(broker)
        try:
            refusal(attempt.handle(resume(capture)))
        finally:
            attempt.close()
        assert json.dumps(capture.host.session.manifest["events"]) == events
        assert len(folders(broker)) == 1 and not capture.finished
    finally:
        current.close()
        capture.stop()


class Control:
    """A raw extension control connection; its reads are bounded by a socket timeout."""

    def __init__(self, path, capabilities=CONTROL, instance="resume-browser"):
        self.connection = connect(path, timeout=5)
        self.stream = self.connection.makefile("rwb")
        send_packet(self.stream, dict(role="extension", browserInstance=instance))
        send_packet(self.stream, dict(type="hello", version=1, capabilities=capabilities))

    def send(self, packet):
        send_packet(self.stream, packet)

    def receive(self):
        return read_packet(self.stream)

    def close(self):
        self.stream.close()
        self.connection.close()


def meeting(**changes):
    return dict(
        dict(
            tabId=3,
            title="Synthetic meeting",
            url="https://meet.example/call",
            sourceCount=1,
            heard=True,
            sourceCountKnown=True,
            callGeneration="call-A",
            documentGeneration="doc-1",
            pageState="present",
        ),
        **changes,
    )


def session(capture, **changes):
    return dict(
        dict(
            recording=True,
            tabId=3,
            captureId=capture.capture_id,
            path=None,
            pageState="present",
            callGeneration="call-A",
            timelineOriginMs=capture.origin_ms,
        ),
        **changes,
    )


@pytest.mark.parametrize("order", ["status-before-native", "native-before-status"])
def test_r67_status_and_native_attach_in_either_order(broker, order):
    """A pending status is an intent: only the native attach plus PCM switches."""
    client, capture = started(broker)
    control = Control(broker.path)
    try:
        control.send(dict(type="meetings", meetings=[meeting()]))
        control.send(dict(type="status", recording=True, sessions=[session(capture)]))
        wait_for(lambda: capture.lifecycle_capable)
        assert client.handle(declare("guest", "doc-1:source:1")) == ACK
        assert client.handle(pcm("guest", 480)) == ACK
        lose(broker, client, capture)
        folder, origin, microphone = capture.directory, capture.origin_ms, capture.microphone
        before = me_frames(capture)
        resumed = proxy(broker)
        pending = session(capture, path=None, tracks=0)
        try:
            if order == "status-before-native":
                control.send(dict(type="status", recording=True, sessions=[pending]))
                (browser,) = broker.service.browsers.values()
                wait_for(lambda: browser.reported.get(3) == "F")
                broker.service.tick()
                assert capture.browser_lost
            assert resumed.handle(resume(capture))["resumed"] is True
            assert capture.browser_lost, "attaching alone never switches the source"
            if order == "native-before-status":
                control.send(dict(type="status", recording=True, sessions=[pending]))
                (browser,) = broker.service.browsers.values()
                wait_for(lambda: browser.reported.get(3) == "F")
                broker.service.tick()
                assert capture.browser_lost
            assert resumed.handle(declare("guest-2", "doc-1:source:1")) == ACK
            assert resumed.handle(pcm("guest-2", 480, offset=now_ms(capture))) == ACK
            # The broker's own policy commits the restore once PCM is fresh.
            broker.service.tick()
            broker.changes.wait(lambda: not capture.browser_lost)
            assert sources(capture) == [("browser", "start"), ("system", "lost"), ("browser", "restored")]
            assert (capture.directory, capture.origin_ms, capture.microphone) == (folder, origin, microphone)
            assert capture.capture_id == "F" and folders(broker) == [folder]
            assert me_frames(capture) >= before
        finally:
            resumed.close()
    finally:
        control.close()
        capture.stop()


def test_r67_stop_before_native_refuses_the_late_resume(broker):
    client, capture = started(broker)
    lose(broker, client, capture)
    broker.service.command("capture:F", "stop", user=True)
    assert capture.finished
    attempt = proxy(broker)
    try:
        refusal(attempt.handle(resume(capture)))
    finally:
        attempt.close()
    assert len(folders(broker)) == 1


def test_r67_stop_during_ack_finalizes_the_target_once(broker):
    client, capture = started(broker)
    lose(broker, client, capture)
    resumed = proxy(broker)
    try:
        replies = [resumed.handle(resume(capture))]
        assert replies[0]["resumed"] is True
        capture.stop()  # The user's Stop crosses the extension's native ACK.
        replies.append(resumed.handle(dict(type="stop", captureId="F", problem=None, discoveredSources=[])))
        assert [reply["type"] for reply in replies] == ["started", "stopped"]
        assert replies[1]["path"] == replies[0]["path"] == str(capture.directory)
        assert len(folders(broker)) == 1
    finally:
        resumed.close()


def test_r67_fresh_record_after_stop_is_an_ordinary_start(broker):
    client, capture = started(broker)
    lose(broker, client, capture)
    broker.service.command("capture:F", "stop", user=True)
    fresh, fresh_capture = started(broker, "G")
    try:
        # A late Stop for F on G's transport cannot finalize G.
        refusal(fresh.handle(dict(type="stop", captureId="F", problem=None, discoveredSources=[])))
        assert not fresh_capture.finished and fresh_capture.capture_id != capture.capture_id
        assert len(folders(broker)) == 2
        assert fresh.handle(dict(type="stop", captureId="G", problem=None, discoveredSources=[]))["type"] == "stopped"
    finally:
        fresh.close()


def test_r67_the_users_stop_names_the_target_so_the_extension_fences_a_queued_resume(broker):
    client, capture = started(broker)
    control = Control(broker.path)
    try:
        control.send(dict(type="meetings", meetings=[meeting()]))
        control.send(dict(type="status", recording=True, sessions=[session(capture)]))
        wait_for(lambda: any(b.states.get(3, {}).get("captureId") == "F" for b in broker.service.browsers.values()))
        (browser,) = broker.service.browsers.values()
        lose(broker, client, capture)
        stopper = threading.Thread(
            target=lambda: broker.service.command(f"{browser.id}:3", "stop", user=True), daemon=True
        )
        stopper.start()
        command = control.receive()
        assert command["action"] == "stop" and command["captureId"] == "F" and "resumeCapture" not in command
        assert capture.finished, "the native target is saved before the browser answers"
        control.send(dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False)))
        stopper.join(5)
        assert not stopper.is_alive()
    finally:
        control.close()


class Native:
    """Chrome's native-messaging child: browser_host.main on real pipes."""

    def __init__(self):
        import os

        from cassini_talk.browser_host import main

        child_in, self._input = os.pipe()
        self._output, child_out = os.pipe()
        self.input, self.output = os.fdopen(self._input, "wb"), os.fdopen(self._output, "rb")
        self.broker_closed = threading.Event()
        self.code = []
        streams = os.fdopen(child_in, "rb"), os.fdopen(child_out, "wb")

        def run():
            try:
                self.code.append(
                    main(
                        open_completed=False,
                        input_stream=streams[0],
                        output_stream=streams[1],
                        broker_closed=self.broker_closed.set,
                    )
                )
            finally:
                for stream in streams:
                    stream.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def handle(self, message):
        from cassini_talk.recording_session import read_message, write_message

        write_message(self.input, message)
        return read_message(self.output)

    def close(self):
        self.input.close()  # The extension closed its port: stdin EOF.
        self.thread.join(5)
        assert not self.thread.is_alive()
        self.output.close()


def test_end_to_end_a_watchdog_close_is_asked_once_and_resumed_on_the_same_folder(broker, monkeypatch):
    from cassini_talk import browser_service
    from cassini_talk.browser_service import PCM_STALE_AFTER

    monkeypatch.setattr(browser_service, "ensure_service", lambda *_a, **_k: broker.path)
    monkeypatch.setattr(browser_service, "service_path", lambda: broker.path)
    service = broker.service
    control = Control(broker.path)
    native = Native()
    resumed = None
    try:
        origin = int(time.time() * 1000)
        reply = native.handle(
            dict(
                type="start",
                version=1,
                independent=True,
                captureId="F",
                timelineOriginMs=origin,
                microphone=True,
                title="Synthetic meeting",
                features=["source-outcomes-v1"],
                callGeneration="call-A",
                documentGeneration="doc-1",
            )
        )
        assert reply == dict(type="started", path=reply["path"], features=["source-outcomes-v1"])
        capture = service.captures["F"]
        control.send(dict(type="meetings", meetings=[meeting()]))
        control.send(dict(type="status", recording=True, sessions=[session(capture, path=reply["path"])]))
        wait_for(lambda: capture.lifecycle_capable and capture.multistream_capable)
        assert native.handle(declare("guest", "doc-1:source:1")) == ACK
        assert native.handle(pcm("guest", 480)) == ACK
        wait_for(Settled(None, service).idle)
        # The companion freezes; the stale-PCM watchdog closes at exactly 45 s.
        broker.clock.now = PCM_STALE_AFTER
        service.tick()
        broker.changes.wait(lambda: capture.browser_lost)
        # Chrome's child notices on its own, with nothing more to relay, so
        # the extension tears the session down now...
        assert native.broker_closed.wait(5)
        assert not native.code, "still serving its port until the process exits"
        # ...and its next status no longer lists the tab.
        control.send(dict(type="status", recording=False, sessions=[]))
        ask = control.receive()
        assert ask == dict(
            type="command",
            requestId=ask["requestId"],
            action="start",
            mode="tracks",
            tabId=3,
            independent=True,
            captureLocal=False,
            callGeneration="call-A",
            keepOnNavigation=True,
            resumeCapture=dict(captureId="F", timelineOriginMs=origin),
        )
        # The extension's new native port carries the resume.
        resumed = proxy(broker)
        assert resumed.handle(resume(capture)) == proof(capture, "source-outcomes-v1")
        control.send(dict(type="result", requestId=ask["requestId"], ok=True, status=session(capture)))
        assert resumed.handle(declare("guest-2", "doc-1:source:1")) == ACK
        assert resumed.handle(pcm("guest-2", 480, offset=now_ms(capture))) == ACK
        service.tick()
        broker.changes.wait(lambda: not capture.browser_lost)
        assert sources(capture) == [("browser", "start"), ("system", "lost"), ("browser", "restored")]
        assert folders(broker) == [capture.directory] and str(capture.directory) == reply["path"]
        assert capture.host.session.capture_record.seen["doc-1:source:1"]["failed"] is False
        (browser,) = service.browsers.values()
        assert not browser.resumes and service._capture_lifecycles["F"].asked_on == {browser.id}
        capture.stop()
    finally:
        if resumed is not None:
            resumed.close()
        native.close()
        control.close()


def test_repeated_resumes_never_mark_a_live_source_failed(broker, caplog):
    """Each loss and resume re-declares the page's roots under fresh span ids.
    Retired spans hold no wire slot, so declarations keep being accepted. The
    session's own 64-track format cap eventually refuses a restore: the call
    then stays on computer sound for that loss, logged once, with no source
    marked failed (SPEC-CONTROL: keep the computer source if a staged
    restoration fails)."""
    import logging

    roots = [f"doc-1:source:{n}" for n in range(4)]
    client, capture = started(broker)
    ledger = capture.host.session.capture_record
    restored_cycles, blocked_cycles = [], []
    try:
        with caplog.at_level(logging.WARNING, logger="cassini_talk.browser_capture"):
            for cycle in range(20):
                for n, root in enumerate(roots):
                    span = f"c{cycle}-s{n}"
                    assert client.handle(declare(span, root)) == ACK, (cycle, root)
                    assert client.handle(pcm(span, 480, offset=now_ms(capture))) == ACK, (cycle, root)
                if cycle:
                    capture.restore()
                    capture.restore()  # A repeated attempt in the same loss logs nothing more.
                    (blocked_cycles if capture.browser_lost else restored_cycles).append(cycle)
                    assert capture.restore_blocked is capture.browser_lost
                assert not any(ledger.seen[root]["failed"] for root in roots), cycle
                lose(broker, client, capture)
                client = proxy(broker)
                assert client.handle(resume(capture))["resumed"] is True
        assert len(capture.host.session.writers) <= 64
        assert restored_cycles == list(range(1, restored_cycles[-1] + 1)) and blocked_cycles
        assert blocked_cycles == list(range(restored_cycles[-1] + 1, 20))
        # Once refused, the recording never left that loss: later transport
        # closes while on computer sound are not new losses.
        assert capture.loss_serial == restored_cycles[-1] + 1
        refused = [r for r in caplog.records if "cannot be restored" in r.getMessage()]
        assert len(refused) == 1, "once per loss, never once per attempt"
        saved = capture.stop()
        assert saved["type"] == "stopped"
        manifest = json.loads((capture.directory / "session.json").read_text())
        outcomes = {s["sourceTrackId"]: s["captureOutcome"] for s in manifest["captureOutcome"]["sources"]}
        assert all(outcomes[root] != "failed" for root in roots)
    finally:
        client.close()
