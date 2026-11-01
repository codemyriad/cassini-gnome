# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual writer fences across address/generation boundaries, without devices."""

import struct
import time
from concurrent.futures import Future

import pytest
from test_browser_capture import Tap

from cassini_talk.browser_capture import BrowserCapture
from cassini_talk.browser_service import BrowserConnection, BrowserService


class HeldWorkers:
    def __init__(self):
        self.tasks = []

    def submit(self, function, *args):
        future = Future()
        self.tasks.append((function, args, future))
        return future

    def drain(self):
        while self.tasks:
            function, args, future = self.tasks.pop(0)
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
                raise


@pytest.mark.parametrize("same_address", [False, True])
def test_call_boundary_fences_pcm_before_worker_then_late_A_cannot_mutate_B(tmp_path, same_address):
    broker = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    broker.workers.shutdown()
    broker.workers = workers = HeldWorkers()
    peer = BrowserConnection(None, browser_instance="generation-test-instance")
    peer.capabilities = {"independent-sessions-v1", "page-state-v1", "call-evidence-v1", "multi-stream-v1"}
    peer.status_seen = True
    broker.browsers[peer.id] = peer
    captures = []
    address_a = "https://meeting.test/A"
    address_b = address_a if same_address else "https://meeting.test/B"

    def start(identifier, generation):
        capture = BrowserCapture(broker._capture_changed, parent=tmp_path / "Library", tap_factory=Tap)
        capture.start(
            dict(
                type="start",
                captureId=identifier,
                callGeneration=generation,
                timelineOriginMs=int(time.time() * 1000),
                microphone=True,
                features=["source-outcomes-v1"],
            )
        )
        broker.captures[identifier] = capture
        peer.states[2] = dict(tabId=2, recording=True, captureId=identifier, pageState="present")
        captures.append(capture)
        return capture

    def inventory(address, generation, count=1):
        broker.update(
            peer,
            dict(
                type="meetings",
                meetings=[
                    dict(
                        tabId=2,
                        title="Synthetic call",
                        url=address,
                        callGeneration=generation,
                        sourceCount=count,
                        sourceCountKnown=True,
                        heard=count > 0,
                        pageState="present",
                    )
                ],
            ),
        )

    try:
        a = start("A", "generation-A")
        inventory(address_a, "generation-A")
        mic = next(t for t in a.host.session.manifest["tracks"] if t["kind"] == "microphone")
        before = mic["frames"]
        inventory(address_b, "generation-B")
        assert a.stop_requested.is_set() and not a.finished
        a.microphone.append(struct.pack("<hhhh", 777, 777, 777, 777), 200)
        assert mic["frames"] == before, "B microphone PCM entered A before the finalizer ran"
        reply = a.handle(dict(type="sources", discoveredSources=[dict(sourceTrackId="late", kind="remote")]))
        assert (reply["type"], reply["code"]) == ("error", "state")
        inventory(address_b, "generation-B")
        assert len(workers.tasks) == 1, "repeated B inventory re-cancelled the same boundary"
        b = start("B", "generation-B")
        inventory(address_b, "generation-B")
        workers.drain()
        assert a.finished and not b.finished
        assert a.host.session.manifest["events"][-1]["reason"] == "callEnded"
        assert not peer.states[2].get("stopping") and peer.states[2]["captureId"] == "B"
        inventory(address_a, "generation-A", 0)
        broker.update(
            peer,
            dict(
                type="status", recording=True, tabId=2, captureId="A", callGeneration="generation-A", pageState="closed"
            ),
        )
        assert peer.meetings[2]["callGeneration"] == "generation-B"
        assert peer.meetings[2]["sourceCount"] == 1
        assert peer.states[2]["captureId"] == "B" and peer.page_states[2] == "present"
        assert not b.stop_requested.is_set()
        # Compatibility tab deltas can omit generation, but still identify A.
        for recording in (True, False):
            broker.update(peer, dict(type="status", recording=recording, tabId=2, captureId="A", pageState="closed"))
            assert peer.states[2]["captureId"] == "B" and peer.page_states[2] == "present"
        mic_b = next(t for t in b.host.session.manifest["tracks"] if t["kind"] == "microphone")
        before_b = mic_b["frames"]
        b.microphone.append(struct.pack("<hhhh", 888, 888, 888, 888), 200)
        assert mic_b["frames"] > before_b
        assert mic["frames"] == before
    finally:
        for capture in captures:
            capture.stop()
        workers.drain()
