# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - the GTK socket reader requires GI.
"""Private meter role, independent sessions, lifecycle and split-line GTK IO."""

import base64
import contextlib
import ctypes.util
import json
import select
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


def test_a_stalled_view_keeps_its_meter_feed_and_then_gets_the_newest_columns():
    """A view whose GTK main loop stops reading (CI: blocked while a slow fallback
    held the capture lock) keeps its live picture. The broker never blocks on
    it and never drops it: columns wait in the bounded lanes, oldest dropped."""
    from types import SimpleNamespace

    from recording.readiness import wait_until

    from cassini_talk.spectro_source import SpectroSource
    from cassini_talk.spectro_transport import serve_meter

    class Rounds(threading.Event):
        """Counts the meter loop's rounds: it checks for shutdown on each."""

        count = 0

        def is_set(self):
            self.count += 1
            return super().is_set()

    source = SpectroSource()
    capture = SimpleNamespace(
        directory="/session",
        finished=False,
        spectrogram=source,
        lock=threading.Lock(),
        _sync_spectrogram=lambda: None,
        elapsed_ms=lambda: 0,
    )
    service = SimpleNamespace(lock=threading.Lock(), captures={"capture": capture}, closed=Rounds())
    broker, view = socket.socketpair(socket.AF_UNIX)
    # The kernel's minimum: a few packets fill it, as ~3 s of packets fill the
    # default one while a stalled view reads nothing.
    broker.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1)
    failures = []

    def run():
        try:
            with broker.makefile("rwb") as stream:
                serve_meter(service, broker, stream, "/session")
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    witness = source.subscribe()
    try:
        wait_until(lambda: len(source.subscribers) == 2, "the view subscribed")
        wait_until(
            lambda: not select.select([], [broker], [], 0)[1] or not worker.is_alive(),
            "the stalled view's socket is full",
            timeout=10,
        )
        # The send that filled the socket may have taken all of its packet; the
        # loop's next round then drains once more to build the packet it cannot
        # send. Once a round has started after that one, nothing drains the
        # view's lane until the view reads again.
        rounds = service.closed.count
        wait_until(
            lambda: service.closed.count >= rounds + 2 or not worker.is_alive(),
            "the meter loop holds a packet its stalled view cannot take",
            timeout=10,
        )
        newest = None
        for n in range(15):
            # 75 columns: more than a lane holds while nothing drains it.
            feed_tone(source, n)
            newest = witness_newest(source, witness, newest)
        assert newest is not None
        rounds = service.closed.count
        # 25 rounds of 40 ms outlast the 0.5 s send bound that used to end the
        # feed; a producer blocked on the view makes no round at all.
        wait_until(
            lambda: service.closed.count >= rounds + 25 or not worker.is_alive(),
            "the meter loop kept running while its view read nothing",
            timeout=10,
        )
        assert worker.is_alive() and len(source.subscribers) == 2, failures
        # Bounded: the stalled view's lane is full and kept the newest columns.
        (stalled,) = source.subscribers - {witness}
        with source.queue_lock:
            lane = list(stalled.lanes["others"])
        assert len(lane) == stalled.lanes["others"].maxlen and lane[-1][0] == newest
        view.setblocking(False)
        buffer, slots = bytearray(), []

        def read():
            with contextlib.suppress(BlockingIOError):
                buffer.extend(view.recv(65536))
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                _now, lanes, _finished = decode_columns(json.loads(line))
                slots.extend(slot for slot, _flags, _bands in lanes["others"])
            return newest in slots

        wait_until(read, "the resumed view received the newest column", timeout=10)
        assert slots == sorted(slots) and not failures
        view.close()
        wait_until(lambda: not worker.is_alive(), "the view's EOF ended its feed", timeout=10)
        assert source.subscribers == {witness} and not failures
    finally:
        view.close()
        service.closed.set()
        worker.join(5)
        broker.close()
    assert not worker.is_alive()


def feed_tone(source, n):
    source.feed(
        "voice",
        tone(4800, amplitude=500),
        n * 100,
        rate=48000,
        channels=1,
        kind="remote",
        start_ms=0,
        now_ms=n * 100,
        level=0.3,
    )


def witness_newest(source, witness, newest):
    """Drains a reading observer so it never overflows; its newest others slot."""
    columns = source.drain(witness, 0)["others"]
    return columns[-1][0] if columns else newest
