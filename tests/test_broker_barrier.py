# SPDX-License-Identifier: GPL-3.0-or-later
"""The no-command proof must finish its producers, not time out quietly."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from broker_barrier import BrokerBarrier, no_command
from test_browser_service import service  # noqa: F401
from test_control_reconnect import peer


@pytest.fixture
def observed(service):  # noqa: F811
    broker, path = service
    BrokerBarrier(broker)
    return broker, path


def test_writer_barrier_preserves_liveness_and_control_facts(observed):
    broker, path = observed
    extension = peer(path)
    browser = next(iter(broker.browsers.values()))
    before = broker.snapshot()["meetings"]
    last = browser.last_inbound
    try:
        assert no_command(broker, extension.connection, extension.stream)
        assert browser.last_inbound == last
        assert broker.snapshot()["meetings"] == before
    finally:
        extension.close()


def test_writer_barrier_does_not_hide_a_queued_command(observed):
    broker, path = observed
    extension = peer(path)
    browser = next(iter(broker.browsers.values()))
    try:
        browser.send(dict(type="command", action="start", requestId="unexpected", tabId=3))
        assert not no_command(broker, extension.connection, extension.stream)
    finally:
        extension.close()


def test_worker_held_until_the_probe_waits_cannot_send_after_the_barrier(observed, monkeypatch):
    broker, path = observed
    extension = peer(path)
    browser = next(iter(broker.browsers.values()))
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    wait = broker.workers.changed.wait

    def observed_wait(timeout=None):
        waiting.set()
        return wait(timeout)

    monkeypatch.setattr(broker.workers.changed, "wait", observed_wait)

    def held():
        entered.set()
        assert release.wait(2)
        browser.send(dict(type="command", action="start", requestId="late-unexpected", tabId=3))

    worker = broker.workers.submit(held)
    try:
        assert entered.wait(2)
        with ThreadPoolExecutor(1) as pool:
            probe = pool.submit(no_command, broker, extension.connection, extension.stream)
            assert waiting.wait(2), "Probe must wait on the held producer's completion event"
            assert not probe.done()
            release.set()
            worker.result(2)
            assert not probe.result(2), "A late producer command must fail the no-command assertion"
    finally:
        release.set()
        extension.close()


def test_input_barrier_waits_for_the_prior_control_handler(service, monkeypatch):  # noqa: F811
    import broker_barrier

    from cassini_talk.browser_service import send_packet

    broker, path = service
    extension = peer(path)
    entered, release, marker_sent = threading.Event(), threading.Event(), threading.Event()
    update = broker.update
    send = broker_barrier.send_packet
    order = []

    def held_update(browser, packet):
        if packet.get("type") == "meetings":
            entered.set()
            assert release.wait(2)
        update(browser, packet)
        order.append("input returned")

    def observed_send(stream, packet):
        send(stream, packet)
        marker_sent.set()

    broker.update = held_update
    BrokerBarrier(broker)
    monkeypatch.setattr(broker_barrier, "send_packet", observed_send)
    try:
        send_packet(extension.stream, dict(type="meetings", meetings=[]))
        assert entered.wait(2)
        with ThreadPoolExecutor(1) as pool:

            def probe():
                result = no_command(broker, extension.connection, extension.stream)
                order.append("probe returned")
                return result

            pending = pool.submit(probe)
            assert marker_sent.wait(2)
            assert not pending.done()
            release.set()
            assert pending.result(2)
        assert order == ["input returned", "probe returned"]
        assert broker.snapshot()["meetings"] == []
    finally:
        release.set()
        extension.close()
