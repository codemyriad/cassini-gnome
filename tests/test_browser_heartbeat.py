# SPDX-License-Identifier: GPL-3.0-or-later
"""heartbeat-v1 (D52.4): literal shared vectors through the broker's real control handler."""

import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

from cassini_talk.browser_service import (
    CONTROL_LOSS_AFTER,
    HEARTBEAT_INTERVAL,
    BrowserService,
    read_packet,
    send_packet,
)

CONTROL = Path(__file__).resolve().parents[1] / "browser-protocol/conformance/control"
VECTORS = json.loads((CONTROL / "heartbeat-vectors.json").read_text())
_oracle = importlib.util.spec_from_file_location("heartbeat_oracle", CONTROL / "heartbeat.py")
oracle = importlib.util.module_from_spec(_oracle)
_oracle.loader.exec_module(oracle)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Link:
    """One extension control connection, served by BrowserService.handle()."""

    def __init__(self, service):
        broker, self.socket = socket.socketpair()
        self.socket.settimeout(5)  # An upper bound for reads, never a wait.
        self.stream = self.socket.makefile("rwb")
        self.thread = threading.Thread(target=service.handle, args=(broker,), daemon=True)
        self.thread.start()
        send_packet(self.stream, dict(role="extension", browserInstance="heartbeat-browser"))

    def close(self):
        self.stream.close()
        self.socket.close()
        self.thread.join(5)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    clock = Clock()
    service = BrowserService(tmp_path / "config.json", notifier=lambda *_: None, clock=clock)
    # Only explicit ticks run: no serve loop. Each frame is awaited as handled.
    handled = threading.Condition()
    count = [0]
    update = service.update

    def counted(browser, packet):
        try:
            update(browser, packet)
        finally:
            with handled:
                count[0] += 1
                handled.notify_all()

    def frame(link, packet):
        expected = count[0] + 1
        send_packet(link.stream, packet)
        with handled:
            assert handled.wait_for(lambda: count[0] >= expected, 5)

    monkeypatch.setattr(service, "update", counted)
    yield service, clock, frame
    service.closed.set()
    service.workers.shutdown(wait=False, cancel_futures=True)


def inbound(kind, step, last_seq):
    if kind == "hello":
        packet = dict(type="hello", version=1)
        if "capabilities" in step:
            packet["capabilities"] = step["capabilities"]
        return packet
    return dict(
        meetings=dict(type="meetings", meetings=[]),
        status=dict(type="status", recording=False, sessions=[]),
        result=dict(type="result", requestId="not-a-pending-command", ok=True),
        pong=dict(type="pong", seq=max(last_seq, 0)),
    )[kind]


def test_heartbeat_constants_match_the_shared_vectors():
    assert VECTORS["constants"] == dict(
        HEARTBEAT_INTERVAL=HEARTBEAT_INTERVAL * 1000, CONTROL_LOSS_AFTER=CONTROL_LOSS_AFTER * 1000
    )


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_shared_heartbeat_vectors_on_the_real_control_handler(broker, case):
    service, clock, frame = broker
    link = browser = None
    try:
        for step in case["steps"]:
            clock.now = step["atMs"] / 1000
            kind = step.get("in")
            if kind == "hello":
                if link is not None:
                    link.close()
                link = Link(service)
            if kind is not None and link is not None:
                frame(link, inbound(kind, step, (browser.ping_seq - 1) if browser else -1))
                if kind == "hello":
                    (browser,) = service.browsers.values()
            sent = browser.ping_seq if browser is not None else 0
            lost = browser is not None and browser.shut_down.is_set()
            service.tick()
            outputs = []
            if browser is not None and browser.shut_down.is_set() and not lost:
                outputs.append("controlLost")
                # The ordinary EOF path runs: the peer is forgotten, which is
                # controlAlive=false for every recording of this browser.
                link.thread.join(5)
                assert not link.thread.is_alive() and browser.id not in service.browsers
                assert read_packet(link.stream) is None
            for seq in range(sent, browser.ping_seq if browser is not None else 0):
                ping = read_packet(link.stream)
                assert ping == dict(type="ping", seq=seq)
                assert oracle.pong_for(ping) == dict(type="pong", seq=seq)
                outputs.append(f"ping:{seq}")
            assert outputs == step["expect"], (case["name"], step)
    finally:
        if link is not None:
            link.close()


def test_heartbeat_loss_takes_the_ordinary_control_loss_path(broker):
    service, clock, frame = broker
    link = Link(service)
    try:
        capabilities = ["independent-sessions-v1", "page-state-v1", "multi-stream-v1", "heartbeat-v1"]
        frame(link, dict(type="hello", version=1, capabilities=capabilities))
        meeting = dict(tabId=3, title="Frozen call", url="https://meet.example/frozen", sourceCount=1, heard=True)
        frame(link, dict(type="meetings", meetings=[meeting]))
        session = dict(recording=True, tabId=3, captureId="frozen-call", pageState="present")
        frame(link, dict(type="status", recording=True, sessions=[session]))
        (browser,) = service.browsers.values()
        assert service.call_captures[("heartbeat-browser", 3)][0] == "frozen-call"
        # The companion freezes: no pong, no frame of any type.
        clock.now = CONTROL_LOSS_AFTER - 0.001
        service.tick()
        assert not browser.shut_down.is_set()
        clock.now = CONTROL_LOSS_AFTER
        service.tick()
        link.thread.join(5)
        assert not link.thread.is_alive() and browser.id not in service.browsers
        reconnect = service._control_reconnects["frozen-call"]
        assert (reconnect.instance, reconnect.tab, reconnect.lifecycle) == ("heartbeat-browser", 3, True)
    finally:
        link.close()


def test_a_returning_frozen_companion_does_not_auto_start_a_second_recording(broker, tmp_path, monkeypatch):
    import time

    from test_browser_capture import Tap

    from cassini_talk.browser_capture import BrowserCapture

    service, clock, frame = broker
    capabilities = ["independent-sessions-v1", "call-evidence-v1", "page-state-v1", "multi-stream-v1", "heartbeat-v1"]
    meeting = dict(
        tabId=3, title="Frozen call", url="https://meet.example/frozen", sourceCount=1, heard=True, callGeneration="g1"
    )
    capture = BrowserCapture(service._capture_changed, parent=tmp_path / "Library", tap_factory=Tap)
    capture.start(dict(type="start", captureId="first", callGeneration="g1", timelineOriginMs=int(time.time() * 1000)))
    service.captures["first"] = capture
    first = Link(service)
    second = None
    try:
        frame(first, dict(type="hello", version=1, capabilities=capabilities))
        frame(first, dict(type="meetings", meetings=[meeting]))
        session = dict(recording=True, tabId=3, captureId="first", callGeneration="g1", pageState="present")
        frame(first, dict(type="status", recording=True, sessions=[session]))
        # Freeze: the audio transport closes onto system/lost, then control is lost.
        capture.close()
        assert capture.browser_lost and not capture.finished
        clock.now = CONTROL_LOSS_AFTER
        service.tick()
        first.thread.join(5)
        assert not first.thread.is_alive()
        service.auto_record = True
        tasks = []
        monkeypatch.setattr(service.workers, "submit", lambda function, *args: tasks.append((function, args)))
        # SIGCONT: the same browser instance returns with the same tab and call.
        second = Link(service)
        frame(second, dict(type="hello", version=1, capabilities=capabilities))
        frame(second, dict(type="meetings", meetings=[meeting]))
        frame(second, dict(type="status", recording=False, sessions=[]))
        service.policy()
        starts = [args for function, args in tasks if function == service._automatic and args[1] == "start"]
        assert not starts, "D52.5: a call on system/lost must not get a second recording"
        assert list(service.captures) == ["first"] and len(list((tmp_path / "Library").iterdir())) == 1
    finally:
        capture.stop()
        first.close()
        if second is not None:
            second.close()
