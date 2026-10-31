# SPDX-License-Identifier: GPL-3.0-or-later
"""Review reproductions: cancellation and addressing across replaceable workers."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_browser_capture import server  # noqa: F401
from test_browser_service import meeting, no_command, service, wait_for  # noqa: F401
from test_control_reconnect import inventory, live, lose, peer

from cassini_viewer.browser_service import request, send_packet


def navigate(extension, url):
    send_packet(
        extension.stream,
        dict(
            type="meetings", meetings=[dict(meeting(), title=extension.title, url=url, pageState="present", heard=True)]
        ),
    )
    wait_for(lambda: any(m["url"] == url for m in request(path=extension.path)["meetings"]))


def test_queued_auto_start_cannot_restart_a_manually_stopped_replacement(service):  # noqa: F811
    broker, path = service
    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]
    for event in entered:
        broker.workers.submit(lambda event=event: (event.set(), release.wait(5)))
    assert all(event.wait(1) for event in entered)
    old = peer(path)
    identifier = request(path=path)["meetings"][0]["id"]
    request("configure", path=path, autoRecord=True)
    old.close()
    wait_for(lambda: not broker.browsers)
    new = peer(path)
    try:
        with ThreadPoolExecutor(1) as pool:
            started = pool.submit(request, "start", path=path, id=identifier)
            new.answer(new.receive())
            started.result(1)
            stopped = pool.submit(request, "stop", path=path, id=identifier)
            new.answer(new.receive())
            stopped.result(1)
        release.set()
        wait_for(lambda: not broker._scheduled_calls)
        assert no_command(new.connection, new.stream), "queued auto work restarted an acknowledged Stop"
    finally:
        release.set()
        new.close()


def test_lost_auto_start_retries_when_its_replacement_arrives_after_real_grace(service):  # noqa: F811
    broker, path = service
    broker.command_timeout = 25
    old = peer(path)
    request("configure", path=path, autoRecord=True)
    assert old.receive()["action"] == "start"
    old.close()
    wait_for(lambda: not broker.browsers)
    end = time.monotonic() + 12
    while broker._scheduled_calls and time.monotonic() < end:
        time.sleep(0.02)
    assert not broker._scheduled_calls
    new = peer(path)
    try:
        command = new.receive()
        assert command["action"] == "start"
        new.answer(command)
        wait_for(lambda: any(s["recording"] for s in request(path=path)["statuses"]))
    finally:
        new.close()


@pytest.mark.parametrize("fallback", [False, True])
def test_record_and_fallback_validate_the_original_address_after_resolution(service, monkeypatch, fallback):  # noqa: F811
    broker, path = service
    extension = peer(path)
    original = request(path=path)["meetings"][0]
    entered, release = threading.Event(), threading.Event()
    resolve = broker._resolve_call

    def paused(*args, **kwargs):
        value = resolve(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return value

    monkeypatch.setattr(broker, "_resolve_call", paused)
    try:
        with ThreadPoolExecutor(1) as pool:
            if fallback:
                recording = pool.submit(broker.start_fallback, original["id"], original)
            else:
                recording = pool.submit(request, "start", path=path, id=original["id"])
            assert entered.wait(1)
            navigate(extension, "https://other.example/new-call")
            release.set()
            with pytest.raises(ValueError, match="no longer available"):
                recording.result(1)
        assert no_command(extension.connection, extension.stream)
        assert not broker.captures
    finally:
        release.set()
        extension.close()


def test_record_all_deduplicates_overlapping_workers_and_uses_public_ids(service):  # noqa: F811
    broker, path = service
    old, new = peer(path), peer(path)
    try:
        with ThreadPoolExecutor(1) as pool:
            recording = pool.submit(request, "start-all", path=path)
            command = new.receive()
            new.answer(command)
            result = recording.result(1)
        assert result["attempted"] == 1 and not result["failures"]
        assert no_command(old.connection, old.stream)
        assert no_command(new.connection, new.stream)
    finally:
        old.close()
        new.close()


def test_enabling_auto_during_worker_overlap_does_not_duplicate_an_active_call(service):  # noqa: F811
    broker, path = service
    old, new = peer(path), peer(path)
    try:
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(1) as pool:
            recording = pool.submit(request, "start", path=path, id=identifier)
            new.answer(new.receive())
            recording.result(1)
        request("configure", path=path, autoRecord=True)
        assert no_command(new.connection, new.stream)
    finally:
        old.close()
        new.close()


def test_a_departed_call_fails_stale_record_immediately_while_browser_is_still_connected(service):  # noqa: F811
    broker, path = service
    broker.command_timeout = 0.3
    extension = peer(path)
    identifier = request(path=path)["meetings"][0]["id"]
    try:
        inventory(extension, ())
        before = time.monotonic()
        with pytest.raises(ValueError, match="meeting is no longer available"):
            request("start", path=path, id=identifier)
        assert time.monotonic() - before < 0.2
    finally:
        extension.close()


def test_single_stop_after_worker_reconnect_saves_retained_audio_without_a_new_worker_session(server):  # noqa: F811
    broker, path, _ = server
    extension, client, capture = live(broker, path)
    identifier = request(path=path)["meetings"][0]["id"]
    lose(broker, extension)
    extension = peer(path)
    try:
        result = request("stop", path=path, id=identifier)
        assert capture.finished and result["stoppedPath"] == str(capture.directory)
        assert all(tap.stopped for _identifier, tap in capture.native_taps.values())
    finally:
        capture.stop()
        extension.close()
        client.close()


def test_stop_keeps_capture_identity_after_navigation_and_tab_disappearance(server):  # noqa: F811
    broker, path, _ = server
    extension, client, capture = live(broker, path)
    identifier = request(path=path)["meetings"][0]["id"]
    try:
        navigate(extension, "https://other.example/new-page")
        inventory(extension, ())
        result = request("stop", path=path, id=identifier)
        assert capture.finished and result["stoppedPath"] == str(capture.directory)
    finally:
        capture.stop()
        extension.close()
        client.close()


def test_stop_cancels_a_recovering_start_even_before_replacement_connects(service):  # noqa: F811
    broker, path = service
    broker.command_timeout = 25
    old = peer(path)
    identifier = request(path=path)["meetings"][0]["id"]
    request("configure", path=path, autoRecord=True)
    assert old.receive()["action"] == "start"
    old.close()
    wait_for(lambda: not broker.browsers)
    result = request("stop", path=path, id=identifier)
    assert not any(s["recording"] for s in result["statuses"])
    wait_for(lambda: not broker._scheduled_calls)
    new = peer(path)
    try:
        assert no_command(new.connection, new.stream)
    finally:
        new.close()


def test_queued_fallback_cannot_run_after_the_call_is_manually_stopped(service, monkeypatch):  # noqa: F811
    broker, path = service
    extension = peer(path)
    entered, release = threading.Event(), threading.Event()
    fallback = broker.start_fallback

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return fallback(*args, **kwargs)

    monkeypatch.setattr(broker, "start_fallback", paused)
    try:
        request("configure", path=path, autoRecord=True)
        extension.answer(extension.receive(), ok=False)
        assert entered.wait(1)
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(1) as pool:
            stopped = pool.submit(request, "stop", path=path, id=identifier)
            extension.answer(extension.receive())
            stopped.result(1)
        release.set()
        wait_for(lambda: not broker._scheduled_calls)
        assert not broker.captures
        assert no_command(extension.connection, extension.stream)
    finally:
        release.set()
        extension.close()


def test_stale_worker_retirement_does_not_reset_the_replacements_failed_attempt(service, monkeypatch):  # noqa: F811
    broker, path = service
    old, new = peer(path), peer(path)

    def unavailable(*args, **kwargs):
        raise ValueError("Synthetic device unavailable")

    monkeypatch.setattr(broker, "start_fallback", unavailable)
    try:
        request("configure", path=path, autoRecord=True)
        new.answer(new.receive(), ok=False)
        wait_for(lambda: not broker._scheduled_calls)
        old.close()
        wait_for(lambda: len(broker.browsers) == 1)
        assert no_command(new.connection, new.stream)
        assert next(iter(broker.browsers.values())).attempted == {3}
    finally:
        new.close()


def test_lifecycle_tab_close_still_notifies_the_extension_after_local_binding_is_retired(server):  # noqa: F811
    broker, path, _ = server
    extension, client, capture = live(broker, path)
    try:
        inventory(extension, ())
        extension.sessions[3]["pageState"] = "closed"
        send_packet(extension.stream, dict(type="status", recording=True, sessions=list(extension.sessions.values())))
        wait_for(lambda: broker._capture_lifecycles[capture.capture_id].closed is not None)
        broker._capture_lifecycles[capture.capture_id].closed = time.monotonic() - 2.1
        broker.policy()
        wait_for(lambda: capture.finished)
        command = extension.receive()
        assert command["action"] == "stop" and command["reason"] == "tabClosed"
        assert command["tabId"] == 3
        extension.answer(command)
        wait_for(lambda: not broker._capture_policy_pending)
    finally:
        capture.stop()
        extension.close()
        client.close()


def test_stale_stop_for_a_previous_address_cannot_erase_the_current_calls_stop(service):  # noqa: F811
    broker, path = service
    extension = peer(path)
    old_id = request(path=path)["meetings"][0]["id"]
    new_url = "https://other.example/current-call"
    navigate(extension, new_url)
    current_id = request(path=path)["meetings"][0]["id"]
    try:
        with ThreadPoolExecutor(1) as pool:
            recording = pool.submit(request, "start", path=path, id=current_id)
            extension.answer(extension.receive())
            recording.result(1)
            stopping = pool.submit(request, "stop", path=path, id=current_id)
            extension.answer(extension.receive())
            stopping.result(1)
        with pytest.raises(ValueError):
            request("stop", path=path, id=old_id)
        assert broker.stopped_calls[("4242:99", 3)][0] == new_url
    finally:
        extension.close()
