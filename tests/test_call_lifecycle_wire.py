# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared packet interleavings through the production broker and writer."""

import json
import time
from pathlib import Path

import pytest
from test_browser_capture import Tap
from test_call_lifecycle import Inline

from cassini_viewer.browser_capture import BrowserCapture
from cassini_viewer.browser_service import BrowserConnection, BrowserService

corpus = json.loads(
    (
        Path(__file__).resolve().parents[1] / "browser-protocol/conformance/control/call-lifecycle-wire-vectors.json"
    ).read_text()
)


@pytest.mark.parametrize("case", corpus["cases"], ids=lambda case: case["name"])
def test_literal_wire_interleaving_uses_production_broker(case, tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    broker = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    broker.workers.shutdown()
    broker.workers = Inline()
    capture = BrowserCapture(broker._capture_changed, parent=tmp_path / "Library", tap_factory=Tap)
    capture.start(
        dict(type="start", captureId="reload-capture", timelineOriginMs=int(time.time() * 1000), microphone=True)
    )
    capture.lifecycle_capable = True
    capture.elapsed_ms = lambda: now[0] * 1000
    capture.handle(dict(type="track", id="initial", kind="remote", sampleRate=48000, channels=1))
    capture.handle(dict(type="audio", id="initial", sequence=0, offsetMs=0, pcm="AQACAA=="))
    broker.captures["reload-capture"] = capture
    browser = BrowserConnection(None)
    browser.browser_instance = "fixture:99"
    browser.capabilities = set(corpus["hello"]["capabilities"])
    browser.status_seen = True
    browser.states = {2: dict(recording=True, tabId=2, captureId="reload-capture", path=str(capture.directory))}
    broker.browsers[browser.id] = browser
    broker.call_captures[("fixture:99", 2)] = ("reload-capture", "https://meet.example/call")
    actions = []
    execute = broker._capture_lifecycle

    def record(*args):
        actions.append(args[-1])
        return execute(*args)

    monkeypatch.setattr(broker, "_capture_lifecycle", record)
    monkeypatch.setattr(broker, "command", lambda *args, **kwargs: {})
    try:
        for index, step in enumerate(case["steps"]):
            now[0] = step["atMs"] / 1000
            packet = step["packet"]
            if (
                packet
                and packet["type"] == "meetings"
                and step["expectEvidence"]["sourceCount"] > 0
                and capture.browser_lost
            ):
                wire = f"returned-{index}"
                capture.handle(dict(type="track", id=wire, kind="remote", sampleRate=48000, channels=1))
                capture.handle(dict(type="audio", id=wire, sequence=0, offsetMs=step["atMs"], pcm="AQACAA=="))
            actions.clear()
            if packet is None:
                broker.policy()
            else:
                broker.update(browser, packet)
            assert actions == step["expect"], step
            meeting = browser.meetings.get(2, {})
            assert (
                dict(
                    pageState=browser.page_states.get(2),
                    sourceCount=meeting.get("sourceCount", 0),
                    heard=meeting.get("heard") is True,
                )
                == step["expectEvidence"]
            ), step
            state = broker._capture_lifecycles["reload-capture"]
            for field, attribute in [("missingAtMs", "missing"), ("endedAtMs", "ended")]:
                actual = getattr(state, attribute)
                expected = step["expectTimers"][field]
                assert actual is None if expected is None else actual * 1000 == pytest.approx(expected), step
    finally:
        capture.stop()
