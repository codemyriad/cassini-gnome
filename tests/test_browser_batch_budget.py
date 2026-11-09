# SPDX-License-Identifier: GPL-3.0-or-later
"""D43.1(c): a batch must fit inside the app RPC's 30-second budget."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from durable import saved
from test_browser_service import service, wait_for  # noqa: F401
from test_browser_sessions import Extension

import cassini_talk.browser_service as browser_service
from cassini_talk.browser_service import request


def fake_deadlines(server, monkeypatch):
    """Command and batch deadlines that pass only when the test expires them,
    however long a loaded host takes to send or answer before that. Returns
    the batch's futures and the expiry."""
    now, futures = [0.0], []

    class Recorded(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            futures.append(future := super().submit(*args, **kwargs))
            return future

    monkeypatch.setattr(browser_service, "ThreadPoolExecutor", Recorded)
    server.deadline_clock = lambda: now[0]

    def expire(seconds=None):
        now[0] += server.batch_timeout + server.command_timeout + 1 if seconds is None else seconds

    return futures, expire


def test_many_unresponsive_starts_share_one_budget_and_a_fast_tab_still_records(service, monkeypatch):  # noqa: F811
    server, path = service
    futures, expire = fake_deadlines(server, monkeypatch)
    # Only the shared batch deadline can end the unanswered Starts.
    server.command_timeout = 30
    peer = Extension(path, tuple(range(1, 9)))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path)
            commands = [peer.receive() for _ in range(8)]
            fast = next(c for c in commands if c["tabId"] == 8)
            peer.answer(fast)
            wait_for(lambda: sum(future.done() for future in futures) == 1)
            expire()
            # Long before any Start's own 30-second wait would end.
            result = pending.result(10)
            assert result["attempted"] == 8 and len(result["failures"]) == 7
            assert {s["tabId"] for s in result["statuses"] if s["recording"]} == {8}
    finally:
        peer.close()


def test_one_unresponsive_stop_does_not_prevent_other_tabs_from_stopping(service, monkeypatch):  # noqa: F811
    from cassini_talk.browser_service import send_packet

    server, path = service
    futures, expire = fake_deadlines(server, monkeypatch)
    server.command_timeout = 30
    peer = Extension(path, tuple(range(1, 5)))
    states = [dict(recording=True, tabId=tab, path=f"/private/recording-{tab}") for tab in range(1, 5)]
    send_packet(peer.stream, dict(type="status", recording=True, sessions=states))
    wait_for(lambda: len(request(path=path)["statuses"]) == 4)
    peer.sessions = {s["tabId"]: s for s in states}
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "stop-all", path=path)
            commands = [peer.receive() for _ in range(4)]
            for command in commands:
                if command["tabId"] != 1:
                    peer.answer(command)
            wait_for(lambda: sum(future.done() for future in futures) == 3)
            expire()
            with pytest.raises((ValueError, TimeoutError)):
                pending.result(10)
            assert {s["tabId"] for s in request(path=path)["statuses"] if s["recording"]} == {1}
    finally:
        peer.close()


def test_more_targets_than_workers_return_a_failure_for_every_unfinished_start(service, monkeypatch):  # noqa: F811
    server, path = service
    _futures, expire = fake_deadlines(server, monkeypatch)
    server.command_timeout = 30
    peer = Extension(path, tuple(range(1, 65)))
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path)
            first = peer.receive()
            assert first["action"] == "start"
            # Half the Starts wait on the browser, half still wait for a worker.
            expire()
            result = pending.result(10)
            # The deadline is not the user's cancel (D58): each unanswered Start
            # is a startFailed/timeout refusal for the start banner.
            assert not result["cancelled"] and result["attempted"] == len(result["failures"]) == 64
            timeout = [dict(atMs=0, side="browser", code="startFailed", detail="timeout")]
            assert all(failure["refusals"] == timeout for failure in result["failures"])
    finally:
        peer.close()


@pytest.mark.parametrize("user", [False, True], ids=["deadline", "user"])
def test_a_start_that_saw_the_deadline_before_its_result_was_read_is_unanswered(service, monkeypatch, user):  # noqa: F811
    server, path = service
    peer = Extension(path, (3,))

    def batch(jobs, _deadline, *, cancelled, expired):
        # The Start saw the cancel (the expiry's or the user's) and ended
        # before the batch read its result, as it can on a loaded host.
        if not user:
            expired.set()
        cancelled.set()
        return [(identifier, None, ValueError("Recording start was cancelled")) for identifier, _job in jobs], []

    monkeypatch.setattr(server, "_batch", batch)
    try:
        result = request("start-all", path=path)
        # D58: only the user's cancel shows no refusal; at the deadline the
        # Start is unanswered, as if it were still running then.
        assert result["cancelled"] is user and len(result["failures"]) == 1
        expected = [] if user else [dict(atMs=0, side="browser", code="startFailed", detail="timeout")]
        assert result["failures"][0]["refusals"] == expected
    finally:
        peer.close()


@pytest.mark.parametrize("audible", [True, False], ids=["audible", "silent"])
def test_a_native_fallback_crossing_the_deadline_is_saved_and_never_left_recording(service, monkeypatch, audible):  # noqa: F811
    import threading

    from test_browser_capture import Tap

    from cassini_talk.browser_capture import BrowserCapture

    server, path = service
    # An audible fallback opens its sides and keeps its folder. A silent one never
    # opens (D58.1: the others stay armed, waiting for sound), so at Stop nothing
    # opened and D58 point 2 leaves no folder. Either way the tab never stays recording.
    if audible:
        monkeypatch.setattr(server, "tap_factory", Tap)
    _futures, expire = fake_deadlines(server, monkeypatch)
    peer = Extension(path, (3,))
    entered, release = threading.Event(), threading.Event()
    start, started = BrowserCapture.start, []

    def delayed(capture, *args, **kwargs):
        started.append(capture)
        entered.set()
        assert release.wait(2)
        return start(capture, *args, **kwargs)

    monkeypatch.setattr(BrowserCapture, "start", delayed)
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request, "start-all", path=path)
            peer.answer(peer.receive(), ok=False)
            assert entered.wait(2)
            expire()
            result = pending.result(10)
            assert not result["cancelled"] and len(result["failures"]) == 1
            assert result["failures"][0]["refusals"][0]["code"] == "startFailed"
            release.set()
            # Its start and save write the folder durably (about 15 fsyncs on a
            # real disk): wait for that save itself, bounded only against a hang.
            (capture,) = started
            saved(capture)
            assert capture.finished
            assert (capture.directory is not None) is audible
            assert any(server.capture_parent.rglob("session.json")) is audible
            assert list(server.captures.values()) == [capture]
            assert not any(s["recording"] for s in request(path=path)["statuses"])
    finally:
        release.set()
        peer.close()


def test_native_audio_is_saved_even_when_another_browser_stop_never_answers(tmp_path):
    import threading

    from test_browser_capture import Tap, bind_capture, remote, start

    from cassini_talk.browser_audio_proxy import BrowserAudioProxy
    from cassini_talk.browser_service import BrowserService, send_packet

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
        start(client, "owned-save", service=server)
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


def test_default_batch_finishes_inside_the_real_thirty_second_request_timeout(service, monkeypatch):  # noqa: F811
    server, path = service
    # Production deadlines run on the monotonic clock fake_deadlines replaces.
    assert server.deadline_clock is time.monotonic
    futures, expire = fake_deadlines(server, monkeypatch)
    server.command_timeout = 25
    timeouts, connections = [], {}
    connect, read = browser_service.connect, browser_service.read_packet

    def connected(*args, **kwargs):
        connections[threading.current_thread()] = connection = connect(*args, **kwargs)
        return connection

    def reading(stream):
        # The socket timeout in effect while request() waits for the reply,
        # whatever request() set after connect().
        if (connection := connections.get(threading.current_thread())) is not None:
            timeouts.append(connection.gettimeout())
        return read(stream)

    # Leave the production batch budget unchanged: two peers never answer,
    # while the third must still start.
    peer = Extension(path, (3, 4, 5))
    # The socket timeout the Record request below really reads its reply with.
    monkeypatch.setattr(browser_service, "connect", connected)
    monkeypatch.setattr(browser_service, "read_packet", reading)
    try:
        with ThreadPoolExecutor(1) as pool:
            before = time.monotonic()
            pending = pool.submit(request, "start-all", path=path)
            commands = [peer.receive() for _ in range(3)]
            peer.answer(next(c for c in commands if c["tabId"] == 5))
            wait_for(lambda: sum(future.done() for future in futures) == 1)
            # Exactly the production budget after the batch began, not a second more.
            expire(server.batch_timeout)
            result = pending.result(10)
            # The budget plus all the real time the request took apart from
            # waiting for it, with 2 s of the socket timeout left (as the 28 s
            # bound of a real-time run gave).
            (timeout,) = timeouts
            assert server.batch_timeout + (time.monotonic() - before) + 2 < timeout
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
