# SPDX-License-Identifier: GPL-3.0-or-later
"""D43.1(a): logical calls outlive their replaceable control transport."""

import threading
from concurrent.futures import ThreadPoolExecutor

from test_browser_service import service, wait_for  # noqa: F401
from test_control_reconnect import inventory, peer

from cassini_viewer.browser_service import request


def test_public_id_and_a_selected_action_follow_the_reconnected_call(service):  # noqa: F811
    server, path = service
    old = peer(path)
    identifier = request(path=path)["meetings"][0]["id"]
    old.close()
    wait_for(lambda: not server.browsers)
    new = peer(path)
    try:
        assert request(path=path)["meetings"][0]["id"] == identifier
        with ThreadPoolExecutor(1) as pool:
            started = pool.submit(request, "start", path=path, id=identifier)
            command = new.receive()
            assert command["tabId"] == 3
            new.answer(command)
            assert not started.result(2).get("error")
    finally:
        new.close()


def test_same_browser_profiles_and_different_browser_processes_have_distinct_ids(service):  # noqa: F811
    _, path = service
    first = peer(path)
    second = peer(path, (7,))
    third = peer(path, instance="4242:100")
    try:
        ids = [m["id"] for m in request(path=path)["meetings"]]
        assert len(ids) == len(set(ids)) == 3
    finally:
        first.close()
        second.close()
        third.close()


def test_queued_automatic_start_follows_late_inventory_without_duplicate_start(service):  # noqa: F811
    server, path = service
    entered = [threading.Event() for _ in range(4)]
    release = threading.Event()
    for event in entered:
        server.workers.submit(lambda event=event: (event.set(), release.wait(3)))
    assert all(event.wait(1) for event in entered)
    old = peer(path)
    identifier = request(path=path)["meetings"][0]["id"]
    request("configure", path=path, autoRecord=True)
    old.close()
    wait_for(lambda: not server.browsers)
    new = peer(path, ())
    try:
        release.set()
        inventory(new, (7,))
        inventory(new, (7, 3))
        commands = [new.receive(), new.receive()]
        assert {c["tabId"] for c in commands} == {3, 7}
        for command in commands:
            new.answer(command)
        wait_for(lambda: not any(b.pending for b in server.browsers.values()))
        assert next(m for m in request(path=path)["meetings"] if m["tabId"] == 3)["id"] == identifier
        assert not server._scheduled_calls
    finally:
        release.set()
        new.close()


def test_navigation_gets_a_new_id_and_an_old_selection_cannot_start_the_new_call(service):  # noqa: F811
    import pytest
    from test_browser_service import meeting

    from cassini_viewer.browser_service import send_packet

    server, path = service
    extension = peer(path)
    try:
        before = request(path=path)["meetings"][0]["id"]
        send_packet(
            extension.stream, dict(type="meetings", meetings=[dict(meeting(), url="https://other.example/new")])
        )
        wait_for(lambda: request(path=path)["meetings"][0]["url"] == "https://other.example/new")
        after = request(path=path)["meetings"][0]["id"]
        assert before != after
        with pytest.raises(ValueError, match="no longer available"):
            server.command(before, "start")
        assert not any(b.pending for b in server.browsers.values())
    finally:
        extension.close()


def test_retiring_stale_owner_does_not_erase_the_replacements_stopped_call(service):  # noqa: F811
    from test_browser_service import meeting

    from cassini_viewer.browser_service import send_packet

    server, path = service
    old = peer(path)
    new = peer(path)
    url = "https://other.example/new"
    try:
        send_packet(new.stream, dict(type="meetings", meetings=[dict(meeting(), url=url)]))
        wait_for(lambda: any(m["url"] == url for m in request(path=path)["meetings"]))
        with server.lock:
            server._store_stop(("4242:99", 3), url)
        old.close()
        wait_for(lambda: len(server.browsers) == 1)
        assert server.stopped_calls[("4242:99", 3)][0] == url
    finally:
        new.close()


def test_pending_start_recovers_its_replacement_status_without_sending_start_twice(service):  # noqa: F811
    from cassini_viewer.browser_service import send_packet

    server, path = service
    old = peer(path)
    identifier = request(path=path)["meetings"][0]["id"]
    with ThreadPoolExecutor(1) as pool:
        started = pool.submit(request, "start", path=path, id=identifier)
        command = old.receive()
        assert command["tabId"] == 3
        old.close()  # The accepted command's response is lost with its worker.
        wait_for(lambda: not server.browsers)
        new = peer(path)
        try:
            assert not started.done(), "a half-discovered replacement prematurely failed the Start"
            state = dict(recording=True, tabId=3, captureId="retained-start", path="/private/recording")
            send_packet(new.stream, dict(type="status", recording=True, sessions=[state]))
            snapshot = started.result(2)
            assert snapshot["statuses"][0]["recording"]
            assert snapshot["meetings"][0]["id"] == identifier
            assert not any(b.pending for b in server.browsers.values())
        finally:
            new.close()


def test_queued_fallback_uses_the_replacement_link_and_keeps_one_recording(service, monkeypatch):  # noqa: F811
    server, path = service
    entered, release = threading.Event(), threading.Event()
    fallback = server.start_fallback

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return fallback(*args, **kwargs)

    monkeypatch.setattr(server, "start_fallback", delayed)
    old = peer(path)
    request("configure", path=path, autoRecord=True)
    old.answer(old.receive(), ok=False)
    assert entered.wait(1)
    old.close()
    wait_for(lambda: not server.browsers)
    new = peer(path)
    try:
        release.set()
        wait_for(lambda: len(server.captures) == 1 and next(iter(server.captures.values())).ready.is_set())
        capture = next(iter(server.captures.values()))
        assert capture.browser_lost and not capture.finished
        assert next(iter(server.browsers.values())).states[3]["captureId"] == capture.capture_id
        assert len(list(server.capture_parent.glob("Recording-*"))) == 1
    finally:
        release.set()
        for capture in list(server.captures.values()):
            capture.stop()
        new.close()


def test_overlapping_workers_publish_one_logical_call_without_rekeying_either_transport(service):  # noqa: F811
    server, path = service
    old = peer(path)
    original_id = request(path=path)["meetings"][0]["id"]
    original_transport = next(iter(server.browsers))
    new = peer(path)
    try:
        snapshot = request(path=path)
        assert len(snapshot["meetings"]) == 1
        assert snapshot["meetings"][0]["id"] == original_id
        assert original_transport in server.browsers and len(server.browsers) == 2
    finally:
        old.close()
        new.close()
