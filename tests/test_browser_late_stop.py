# SPDX-License-Identifier: GPL-3.0-or-later
"""R2: late wire results/audio must not outlive an acknowledged Stop intent."""

import contextlib
import threading
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_browser_attempt_reset import announce
from test_browser_capture import server, start  # noqa: F401
from test_browser_service import no_command, service, wait_for  # noqa: F401
from test_control_reconnect import peer

from cassini_viewer.browser_audio_proxy import BrowserAudioProxy
from cassini_viewer.browser_service import request, send_packet


def state(extension, *, capture_id="late", path="/private/Recording-late"):
    value = dict(recording=True, tabId=3, captureId=capture_id, path=path, pageState="present")
    extension.sessions[3] = value
    send_packet(extension.stream, dict(type="status", recording=True, sessions=[value]))
    return value


def result(extension, command, value):
    send_packet(extension.stream, dict(type="result", requestId=command["requestId"], ok=True, status=value))


@pytest.mark.parametrize("with_audio", [False, True])
def test_batch_timeout_retires_a_late_start_and_its_status_or_audio(server, with_audio):  # noqa: F811
    broker, path, _ = server
    broker.batch_timeout, broker.command_timeout = 0.2, 1
    extension = peer(path)
    client = BrowserAudioProxy(path=path) if with_audio else None
    capture = None
    try:
        with ThreadPoolExecutor(1) as pool:
            batch = pool.submit(request, "start-all", path=path)
            starting = extension.receive()
            stopping = extension.receive()
            assert (starting["action"], stopping["action"]) == ("start", "stop")
            response = batch.result(1)
            assert response["failures"] and not response["statuses"]
            if client is not None:
                started = start(client, "late")
                capture = broker.captures["late"]
                directory = started["path"]
            else:
                directory = "/private/Recording-late"
            active = state(extension, path=directory)
            result(extension, starting, dict(active, sessions=[active]))
            extension.answer(stopping)
            extension.title += " after-late-result"
            announce(extension)
        wait_for(lambda: not any(s["recording"] for s in request(path=path)["statuses"]))
        if capture is not None:
            wait_for(lambda: capture.finished)
            track = capture.host.session.manifest["tracks"][0]
            with wave.open(str(capture.directory / track["file"])) as audio:
                assert audio.readframes(audio.getnframes()).endswith(b"\x64\x00\xc8\x00\x2c\x01\x90\x01")
        wait_for(lambda: not any(b.pending for b in broker.browsers.values()))
    finally:
        if capture is not None:
            capture.stop()
        extension.close()
        if client is not None:
            client.close()


@pytest.mark.parametrize("with_audio", [False, True])
def test_stop_during_recovery_retires_the_start_a_replacement_reports_as_recording(server, with_audio):  # noqa: F811
    broker, path, _ = server
    broker.command_timeout = 3
    old = peer(path)
    replacement = None
    client = None
    capture = None
    try:
        identifier = request(path=path)["meetings"][0]["id"]
        request("configure", path=path, autoRecord=True)
        assert old.receive()["action"] == "start"
        old.close()
        wait_for(lambda: not broker.browsers)
        request("stop", path=path, id=identifier)
        wait_for(lambda: not broker._scheduled_calls)
        if with_audio:
            client = BrowserAudioProxy(path=path)
            start(client, "late")
            capture = broker.captures["late"]
        replacement = peer(path, ())
        state(replacement, path=str(capture.directory) if capture else "/private/Recording-late")
        announce(replacement)
        stopping = replacement.receive()
        assert stopping["action"] == "stop"
        replacement.answer(stopping)
        wait_for(lambda: not any(s["recording"] for s in request(path=path)["statuses"]))
        if capture is not None:
            wait_for(lambda: capture.finished)
        assert no_command(replacement.connection, replacement.stream)
    finally:
        if capture is not None:
            capture.stop()
        old.close()
        if replacement is not None:
            replacement.close()
        if client is not None:
            client.close()


def test_capture_opened_after_stop_dispatch_is_saved_when_its_identity_arrives(server):  # noqa: F811
    broker, path, _ = server
    broker.command_timeout = 1
    extension = peer(path)
    client = BrowserAudioProxy(path=path)
    capture = None
    try:
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(2) as pool:
            starting = pool.submit(request, "start", path=path, id=identifier)
            start_command = extension.receive()
            stopping = pool.submit(request, "stop", path=path, id=identifier)
            stop_command = extension.receive()
            start(client, "late")
            capture = broker.captures["late"]
            active = state(extension, path=str(capture.directory))
            result(extension, start_command, dict(active, sessions=[active]))
            extension.answer(stop_command)
            saved = stopping.result(1)
            with contextlib.suppress(ValueError):
                starting.result(1)
        wait_for(lambda: capture.finished)
        assert saved["stoppedPath"] == str(capture.directory)
    finally:
        if capture is not None:
            capture.stop()
        extension.close()
        client.close()


def test_non_user_quit_stop_joins_an_already_pending_user_stop(service):  # noqa: F811
    broker, path = service
    broker.command_timeout = 2
    extension = peer(path)
    try:
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(2) as pool:
            starting = pool.submit(request, "start", path=path, id=identifier)
            extension.answer(extension.receive())
            starting.result(1)
            user_stop = pool.submit(request, "stop", path=path, id=identifier)
            stopping = extension.receive()
            quit_stop = pool.submit(broker.command, identifier, "stop", user=False, reason="appQuit")
            done = threading.Event()
            quit_stop.add_done_callback(lambda _: done.set())
            assert not done.wait(0.1), "Quit must join the pending Stop until its acknowledgement"
            extension.answer(stopping)
            assert not any(s["recording"] for s in user_stop.result(1)["statuses"])
            assert not any(s["recording"] for s in quit_stop.result(1)["statuses"])
        assert no_command(extension.connection, extension.stream)
    finally:
        extension.close()


def test_stop_all_deduplicates_a_logical_status_and_pending_transport_start(service, monkeypatch):  # noqa: F811
    broker, path = service
    extension = peer(path)
    identifier = request(path=path)["meetings"][0]["id"]
    active = state(extension)
    wait_for(lambda: any(s.get("captureId") == active["captureId"] for s in request(path=path)["statuses"]))
    browser = next(iter(broker.browsers.values()))
    pending = dict(
        action="start", tabId=3, event=threading.Event(), result=None, logical=identifier, version=0, generation=0
    )
    # Hold a valid accepted-but-unacknowledged Start at the batch boundary.
    # No waiter retires it while Stop-all snapshots both representations.
    with broker.lock:
        browser.pending["held-start"] = pending
        broker._pending_calls.setdefault(identifier, {})["held-start"] = pending
    jobs = []
    batch = broker._batch

    def record(work, *args, **kwargs):
        jobs.extend(key for key, _fn in work)
        return batch(work, *args, **kwargs)

    monkeypatch.setattr(broker, "_batch", record)
    try:
        with ThreadPoolExecutor(1) as pool:
            stopped = pool.submit(request, "stop-all", path=path)
            extension.answer(extension.receive())
            assert not any(s["recording"] for s in stopped.result(1)["statuses"])
        assert jobs == [identifier], "one call must have one stop job"
    finally:
        with broker.lock:
            browser.pending.pop("held-start", None)
            broker._pending_calls.get(identifier, {}).pop("held-start", None)
        extension.close()


def test_late_old_stop_ack_cannot_hide_a_newer_accepted_recording(service):  # noqa: F811
    broker, path = service
    broker.command_timeout = 0.1
    extension = peer(path)
    try:
        identifier = request(path=path)["meetings"][0]["id"]
        with ThreadPoolExecutor(1) as pool:
            started = pool.submit(request, "start", path=path, id=identifier)
            extension.answer(extension.receive())
            started.result(1)
            stopped = pool.submit(request, "stop", path=path, id=identifier)
            old_stop = extension.receive()
            with pytest.raises(ValueError):
                stopped.result(1)
            newer = pool.submit(request, "start", path=path, id=identifier)
            extension.answer(extension.receive())
            newer.result(1)
        current = extension.sessions[3]["captureId"]
        result(extension, old_stop, dict(recording=False, tabId=3, sessions=[]))
        extension.title += " after-old-stop"
        announce(extension)
        wait_for(lambda: request(path=path)["meetings"][0]["recording"])
        assert any(s.get("captureId") == current and s["recording"] for s in request(path=path)["statuses"])
    finally:
        extension.close()
