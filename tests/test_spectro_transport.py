# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - the GTK socket reader requires GI.
"""Private meter role, independent sessions, lifecycle and split-line GTK IO."""

import base64
import ctypes.util
import json
import socket
import threading

import pytest

pytest.importorskip("gi")
from gi.repository import GLib
from test_browser_capture import server as server
from test_browser_capture import start
from test_recorder import spin_until
from test_spectro_analysis import tone

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.spectro_transport import MeterClient, decode_columns


@pytest.fixture(autouse=True)
def real_fft():
    if ctypes.util.find_library("gstfft-1.0") is None:
        pytest.skip("Install GStreamer base for the recording/GUI gate")


def track(proxy):
    proxy.handle(dict(type="track", id="wire", label="Other", kind="remote", sampleRate=48000, channels=1))


def pcm(proxy, sequence=0, *, offset_ms=None):
    proxy.handle(
        dict(
            type="audio",
            id="wire",
            sequence=sequence,
            offsetMs=100 * sequence if offset_ms is None else offset_ms,
            pcm=base64.b64encode(tone(4800, amplitude=500)).decode(),
        )
    )


def test_two_views_share_analysis_of_one_session_and_unmap_stops_it(server):
    service, path, _root = server
    one, other = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    first = start(one, "first", microphone=False)
    start(other, "unobserved", microphone=False)
    track(one)
    track(other)
    received = [[], []]
    clients = [
        MeterClient(path, first["path"], lambda _now, lanes, i=i: received[i].extend(lanes["others"])) for i in range(2)
    ]
    source = service.captures["first"].spectrogram
    try:
        spin_until(lambda: len(source.subscribers) == 2)
        pcm(one)
        pcm(other)
        spin_until(lambda: all(received))
        assert received[0] == received[1]
        assert service.captures["unobserved"].spectrogram.calls == 0
        clients[0].close()
        spin_until(lambda: len(source.subscribers) == 1)
        clients[1].close()
        spin_until(lambda: not source.active)
        before = source.calls
        pcm(one, 1)
        assert source.calls == before and not source.tracks
    finally:
        for client in clients:
            client.close()
        one.handle(dict(type="stop"))
        other.handle(dict(type="stop"))
        one.close()
        other.close()


def test_finished_session_closes_meter_and_releases_its_observer(server):
    service, path, _root = server
    proxy = BrowserAudioProxy(path=path)
    created = start(proxy, "first", microphone=False)
    client = MeterClient(path, created["path"], lambda *_: None)
    source = service.captures["first"].spectrogram
    try:
        spin_until(lambda: source.active)
        proxy.handle(dict(type="stop"))
        spin_until(lambda: client.finished and not source.subscribers)
        assert client.problem is None and client.watch == 0
    finally:
        client.close()
        proxy.close()


def test_unknown_directory_does_not_allocate_an_analyser(server, tmp_path):
    _service, path, _root = server
    closed = []
    client = MeterClient(
        path, tmp_path / "outside", lambda *_: pytest.fail("Unknown session sent columns"), closed.append
    )
    try:
        spin_until(lambda: bool(closed))
        assert client.problem and client.watch == 0
    finally:
        client.close()


def test_split_json_line_does_not_block_the_gtk_main_loop(tmp_path):
    path = tmp_path / "meter.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    suffix = threading.Event()
    failures = []
    packet = dict(
        type="columns",
        nowMs=100,
        finished=True,
        lanes={"me": [], "others": [[3, 0, base64.b64encode(bytes(32)).decode()]]},
    )
    data = (json.dumps(packet) + "\n").encode()

    def writer():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(3)
                with connection.makefile("rb") as reader:
                    assert json.loads(reader.readline())["role"] == "meter"
                connection.sendall(data[:40])
                assert suffix.wait(3), "GTK did not dispatch an idle while its JSON line was incomplete"
                connection.sendall(data[40:])
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=writer, daemon=True)
    worker.start()
    received = []
    client = MeterClient(path, tmp_path / "session", lambda now, lanes: received.append((now, lanes)))
    try:
        spin_until(lambda: bool(client.buffer))
        assert not received
        GLib.idle_add(lambda: suffix.set() or False)
        spin_until(lambda: client.finished)
        assert received == [(100, {"me": [], "others": [(3, 0, bytes(32))]})]
        assert not failures
    finally:
        suffix.set()
        client.close()
        worker.join(timeout=3)
        listener.close()
        assert not worker.is_alive()


@pytest.mark.parametrize(
    "field,value",
    [
        ("nowMs", float("nan")),
        ("nowMs", True),
        ("lanes", {}),
        ("lanes", {"me": [], "others": [[True, 0, "A" * 44]]}),
        ("lanes", {"me": [], "others": [[1, 0, "!"]]}),
        ("lanes", {"me": [], "others": [[1, 0, "A" * 44]] * 65}),
    ],
)
def test_invalid_or_unbounded_meter_data_is_rejected(field, value):
    packet = dict(type="columns", nowMs=100, lanes={"me": [], "others": []})
    packet[field] = value
    with pytest.raises(ValueError):
        decode_columns(packet)


def test_a_broken_view_callback_releases_the_producer_without_stopping_audio(server):
    service, path, _root = server
    proxy = BrowserAudioProxy(path=path)
    created = start(proxy, "first", microphone=False)
    track(proxy)

    def broken(_now, lanes):
        if lanes["others"]:
            raise RuntimeError("Synthetic view failure")

    client = MeterClient(path, created["path"], broken)
    capture = service.captures["first"]
    try:
        spin_until(lambda: capture.spectrogram.active)
        pcm(proxy)
        spin_until(lambda: client.problem is not None and not capture.spectrogram.active)
        assert client.problem == "Synthetic view failure" and client.watch == 0
        assert not capture.finished and capture.error is None
        pcm(proxy, 1)
    finally:
        client.close()
        proxy.handle(dict(type="stop"))
        proxy.close()


@pytest.mark.parametrize("initial_ms", [0, 1000])
def test_window_mapping_controls_real_broker_feed_without_stopping_capture(server, monkeypatch, initial_ms):
    from recording.readiness import wait_until
    from test_spectro_views import Strip, Window

    from cassini_talk.spectro_views import BrokerFeed, WindowSpectrograms

    service, path, _root = server
    proxy = BrowserAudioProxy(path=path)
    created = start(proxy, "mapped-feed", microphone=False)
    track(proxy)
    capture = service.captures["mapped-feed"]
    clock = [initial_ms]
    monkeypatch.setattr(capture, "elapsed_ms", lambda: clock[0])
    window, strip = Window(), Strip()
    hub = WindowSpectrograms(window)
    meter_ready, columns_ready = threading.Event(), threading.Event()

    def feed(columns):
        def received(now, lanes):
            columns(now, lanes)
            meter_ready.set()
            if lanes["others"]:
                columns_ready.set()

        return BrokerFeed(path, created["path"], received, clock=lambda: clock[0])

    def diagnostics():
        view = hub.sessions.get(created["path"])
        return dict(
            clockMs=clock[0],
            meterReady=meter_ready.is_set(),
            columnsReady=columns_ready.is_set(),
            clientProblem=getattr(getattr(view.feed, "client", None), "problem", None) if view else None,
            sourceProblem=capture.spectrogram.problem,
            analysisCalls=capture.spectrogram.calls,
            analysedColumns=capture.spectrogram.analysed,
        )

    try:
        hub.bind(strip, created["path"], feed)
        wait_until(meter_ready.is_set, "real meter subscribed and delivered its first packet", diagnostics=diagnostics)
        window.frame(clock[0])
        # Keep synthetic PCM, the broker and the fake frame clock in the same
        # time domain. Startup/IPC scheduling must not age out a single packet
        # at offset zero before the test has observed its column-delivery event.
        pcm(proxy, offset_ms=clock[0])
        wait_until(
            columns_ready.is_set, "PCM columns reached the view through the real socket", diagnostics=diagnostics
        )
        clock[0] += 200
        window.frame(clock[0])
        assert strip.model is not None and any(strip.model.bands["others"]), diagnostics()
        assert len(capture.spectrogram.subscribers) == 1
        window.mapped = False
        window.emit("unmap")
        wait_until(lambda: not capture.spectrogram.subscribers, "unmapped view released its broker subscription")
        assert not window.ticks and strip.model is None
        before = capture.spectrogram.calls
        pcm(proxy, 1, offset_ms=clock[0])
        assert capture.spectrogram.calls == before and not capture.finished
    finally:
        hub.close()
        proxy.handle(dict(type="stop"))
        proxy.close()
