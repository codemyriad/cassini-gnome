# SPDX-License-Identifier: GPL-3.0-or-later
"""D43.1(c): a batch must fit inside the app RPC's 30-second budget."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_browser_service import service, wait_for  # noqa: F401
from test_browser_sessions import Extension

from cassini_viewer.browser_service import request


def test_many_unresponsive_starts_share_one_budget_and_a_fast_tab_still_records(service):  # noqa: F811
    server, path = service
    server.batch_timeout = 0.45
    peer = Extension(path, tuple(range(1, 9)))
    try:
        with ThreadPoolExecutor(1) as pool:
            before = time.monotonic()
            pending = pool.submit(request, "start-all", path=path)
            commands = [peer.receive() for _ in range(8)]
            fast = next(c for c in commands if c["tabId"] == 8)
            peer.answer(fast)
            result = pending.result(1)
            assert time.monotonic() - before < 0.8
            assert result["attempted"] == 8 and len(result["failures"]) == 7
            assert {s["tabId"] for s in result["statuses"] if s["recording"]} == {8}
    finally:
        peer.close()


def test_one_unresponsive_stop_does_not_prevent_other_tabs_from_stopping(service):  # noqa: F811
    from cassini_viewer.browser_service import send_packet

    server, path = service
    server.batch_timeout = 0.45
    peer = Extension(path, tuple(range(1, 5)))
    states = [dict(recording=True, tabId=tab, path=f"/private/recording-{tab}") for tab in range(1, 5)]
    send_packet(peer.stream, dict(type="status", recording=True, sessions=states))
    wait_for(lambda: len(request(path=path)["statuses"]) == 4)
    peer.sessions = {s["tabId"]: s for s in states}
    try:
        with ThreadPoolExecutor(1) as pool:
            before = time.monotonic()
            pending = pool.submit(request, "stop-all", path=path)
            commands = [peer.receive() for _ in range(4)]
            for command in commands:
                if command["tabId"] != 1:
                    peer.answer(command)
            with pytest.raises((ValueError, TimeoutError)):
                pending.result(1)
            assert time.monotonic() - before < 0.8
            assert {s["tabId"] for s in request(path=path)["statuses"] if s["recording"]} == {1}
    finally:
        peer.close()


def test_more_targets_than_workers_return_a_failure_for_every_unfinished_start(service):  # noqa: F811
    server, path = service
    server.batch_timeout = 0.2
    server.command_timeout = 2
    peer = Extension(path, tuple(range(1, 65)))
    try:
        with ThreadPoolExecutor(1) as pool:
            before = time.monotonic()
            pending = pool.submit(request, "start-all", path=path)
            first = peer.receive()
            assert first["action"] == "start"
            result = pending.result(1)
            assert time.monotonic() - before < 0.8
            assert result["cancelled"] and result["attempted"] == len(result["failures"]) == 64
    finally:
        peer.close()


def test_a_native_fallback_crossing_the_deadline_is_saved_and_never_left_recording(service, monkeypatch):  # noqa: F811
    import threading

    from cassini_viewer.browser_capture import BrowserCapture

    server, path = service
    server.batch_timeout = 0.15
    peer = Extension(path, (3,))
    entered, release = threading.Event(), threading.Event()
    start = BrowserCapture.start

    def delayed(capture, *args, **kwargs):
        entered.set()
        assert release.wait(2)
        return start(capture, *args, **kwargs)

    monkeypatch.setattr(BrowserCapture, "start", delayed)
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path)
            peer.answer(peer.receive(), ok=False)
            assert entered.wait(1)
            result = pending.result(1)
            assert result["cancelled"] and len(result["failures"]) == 1
            release.set()
            wait_for(lambda: server.captures and all(c.finished for c in server.captures.values()))
            assert not any(s["recording"] for s in request(path=path)["statuses"])
    finally:
        release.set()
        peer.close()


def test_native_audio_is_saved_even_when_another_browser_stop_never_answers(tmp_path):
    import threading

    from test_browser_capture import Tap, bind_capture, remote, start

    from cassini_viewer.browser_audio_proxy import BrowserAudioProxy
    from cassini_viewer.browser_service import BrowserService, send_packet

    path = tmp_path / "broker/browser.sock"
    server = BrowserService(
        tmp_path / "settings.json", command_timeout=0.3, capture_parent=tmp_path / "Library", tap_factory=Tap
    )
    server.batch_timeout = 0.45
    ready = threading.Event()
    thread = threading.Thread(target=server.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(2)
    peer = Extension(path)
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "owned-save")
        remote(client, "voice")
        capture = server.captures["owned-save"]
        saved = {
            capture.directory / track["file"]: (capture.directory / track["file"]).read_bytes()[44:]
            for track in capture.host.session.manifest["tracks"]
        }
        bind_capture(peer, capture)
        peer.sessions[4] = dict(recording=True, tabId=4, path="/private/unresponsive")
        send_packet(peer.stream, dict(type="status", recording=True, sessions=list(peer.sessions.values())))
        wait_for(lambda: len(request(path=path)["statuses"]) == 2)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "stop-all", path=path)
            commands = [peer.receive(), peer.receive()]
            for command in commands:
                if command["tabId"] == 3:
                    peer.answer(command)
            with pytest.raises((ValueError, TimeoutError)):
                pending.result(1)
        assert capture.finished and all(track["ended"] for track in capture.host.session.manifest["tracks"])
        assert capture.host.session.manifest["events"][-1]["reason"] == "user"
        assert all(tap.stopped for _identifier, tap in capture.native_taps.values())
        assert any(saved.values())
        assert all(path.read_bytes()[44:].startswith(pcm) for path, pcm in saved.items())
    finally:
        client.close()
        peer.close()
        server.closed.set()
        thread.join(2)
        assert not thread.is_alive()


def test_stop_cannot_overtake_a_start_then_leave_that_start_recording(service, monkeypatch):  # noqa: F811
    import threading

    server, path = service
    peer = Extension(path, (3,))
    connection = next(iter(server.browsers.values()))
    entered, release = threading.Event(), threading.Event()
    send = connection.send

    def delayed(packet):
        if packet["action"] == "start":
            entered.set()
            assert release.wait(2)
        send(packet)

    monkeypatch.setattr(connection, "send", delayed)
    try:
        with ThreadPoolExecutor(2) as pool:
            starting = pool.submit(request, "start-all", path=path)
            assert entered.wait(1)
            stopping = pool.submit(request, "stop-all", path=path)
            wait_for(lambda: any(p.get("cancelled") for p in connection.pending.values()))
            release.set()
            first, second = peer.receive(), peer.receive()
            assert (first["action"], second["action"]) == ("start", "stop")
            peer.answer(first)
            peer.answer(second)
            assert starting.result(1)["cancelled"]
            assert not stopping.result(1)["statuses"]
            assert not connection.pending
    finally:
        release.set()
        peer.close()


def test_default_batch_finishes_inside_the_real_thirty_second_request_timeout(service):  # noqa: F811
    server, path = service
    server.command_timeout = 25
    # Leave the production batch budget unchanged: two peers never answer,
    # while the third must still start, using request()'s actual 30 s socket.
    peer = Extension(path, (3, 4, 5))
    try:
        with ThreadPoolExecutor(1) as pool:
            before = time.monotonic()
            pending = pool.submit(request, "start-all", path=path)
            commands = [peer.receive() for _ in range(3)]
            peer.answer(next(c for c in commands if c["tabId"] == 5))
            result = pending.result(28)
            assert time.monotonic() - before < 28
            assert result["attempted"] == 3 and len(result["failures"]) == 2
            assert {s["tabId"] for s in result["statuses"] if s["recording"]} == {5}
    finally:
        peer.close()


def test_a_start_acknowledged_only_after_its_timeout_is_stopped_and_reported_failed(service):  # noqa: F811
    server, path = service
    server.batch_timeout = 0.6
    peer = Extension(path, (3,))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path)
            start = peer.receive()
            stop = peer.receive()
            assert (start["action"], stop["action"]) == ("start", "stop")
            peer.answer(start)
            peer.answer(stop)
            result = pending.result(1)
            assert len(result["failures"]) == 1
            assert not any(s["recording"] for s in result["statuses"])
            assert not any(b.pending for b in server.browsers.values())
    finally:
        peer.close()
