# SPDX-License-Identifier: GPL-3.0-or-later
"""Record right after Stop, on the real broker: the new session starts while the old one saves."""

import threading
from concurrent.futures import ThreadPoolExecutor

from broker_fixture import stop_broker
from durable import saved
from test_browser_capture import Tap, bind_capture, remote, start
from test_browser_service import wait_for
from test_browser_sessions import Extension
from test_refusal_writer import recordings_started
from test_voice_application import application_class  # noqa: F401

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.browser_service import BrowserService, request
from cassini_talk.library_v2 import finalize


def tab_status(socket, tab=3):
    return next(s for s in request(path=socket)["statuses"] if s.get("tabId") == tab)


def broker(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    service = BrowserService(tmp_path / "broker-config.json", tap_factory=Tap, notifier=lambda *_args: None)
    socket, ready = tmp_path / "broker/service.sock", threading.Event()
    worker = threading.Thread(target=service.serve, args=(socket, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    return service, socket, worker


def test_record_after_stop_starts_while_the_stop_is_unanswered_and_publication_is_held(
    tmp_path,
    monkeypatch,
    application_class,  # noqa: F811
):
    entered, release = threading.Event(), threading.Event()
    original = finalize.finalize

    def held_publication(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", held_publication)
    service, socket, worker = broker(tmp_path, monkeypatch)
    peer = Extension(socket, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=socket)
    try:
        start(client, "old", service=service)
        remote(client, "remote")
        old = service.captures["old"]
        bind_capture(peer, old)
        with ThreadPoolExecutor(2) as pool:
            stopped = pool.submit(request, "stop-all", path=socket)
            stop = peer.receive()  # The browser's Stop acknowledgement is held...
            assert stop["action"] == "stop" and stop["tabId"] == 3
            assert entered.wait(5)  # ...and so is the old capture's publication.
            assert old.stopping and not old.finished
            # The raw browser row still says recording; the one per-capture
            # phase the app and start_all share says it only saves.
            assert tab_status(socket)["phase"] == "stopping" and not tab_status(socket)["recording"]

            started = pool.submit(request, "start-all", path=socket, groupId="second-record")
            wait_for(lambda: "second-record" in service._record_groups)  # Queued behind the Stop.
            peer.answer(stop)
            new = peer.receive()  # Sent only once the Stop is answered.
            assert new["action"] == "start" and new["tabId"] == 3
            peer.answer(new)
            result = started.result(5)
            assert stopped.result(5)["stoppedPaths"] == [str(old.directory)]

        assert result["attempted"] == 1 and result["failures"] == [] and result["skipped"] == []
        live = next(s for s in result["statuses"] if s.get("tabId") == 3)
        assert live["phase"] == "recording" and live["captureId"] != "old"
        assert old.stopping and not old.finished  # The old one is still saving.
        assert recordings_started(application_class, result).recording_failure is None

        release.set()
        saved(old)  # Its publication commits durably.
        # The old capture's publication must not mark the new session stopped.
        assert tab_status(socket)["captureId"] == live["captureId"] and tab_status(socket)["phase"] == "recording"
    finally:
        release.set()
        peer.close()
        client.close()
        stop_broker(service, worker, socket)
        for capture in service.captures.values():
            saved(capture)


def test_start_all_that_skips_every_live_call_says_so(tmp_path, monkeypatch, application_class):  # noqa: F811
    service, socket, worker = broker(tmp_path, monkeypatch)
    peer = Extension(socket, (3,), instance="4242:99")
    client = BrowserAudioProxy(path=socket)
    try:
        start(client, "live", service=service)
        remote(client, "remote")
        bind_capture(peer, service.captures["live"])
        result = request("start-all", path=socket, groupId="while-live")
        assert result["attempted"] == 0 and result["failures"] == []
        assert [(s["reason"], s["title"]) for s in result["skipped"]] == [("recording", "Synthetic meeting")]
        # Record never does nothing silently: the start banner names it busy.
        failure = recordings_started(application_class, result).recording_failure
        assert failure is not None and failure["caption"] == "errors.refusal.busy"
    finally:
        peer.close()
        client.close()
        stop_broker(service, worker, socket)
        for capture in service.captures.values():
            saved(capture)
