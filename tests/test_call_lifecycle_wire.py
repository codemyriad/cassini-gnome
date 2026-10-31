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


@pytest.mark.parametrize("status_style", ["shared", "tab-scoped"])
@pytest.mark.parametrize("instance", [None, "fixture:99"], ids=["without-instance", "with-instance"])
@pytest.mark.parametrize("case", corpus["cases"], ids=lambda case: case["name"])
def test_literal_wire_interleaving_uses_production_broker(case, instance, status_style, tmp_path, monkeypatch):
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
    browser.browser_instance = instance
    browser.capabilities = set(corpus["hello"]["capabilities"])
    browser.status_seen = True
    browser.states = {2: dict(recording=True, tabId=2, captureId="reload-capture", path=str(capture.directory))}
    broker.browsers[browser.id] = browser
    if instance is not None:
        broker.call_captures[(instance, 2)] = ("reload-capture", "https://meet.example/call")
    actions = []
    folder, microphone = capture.directory, capture.microphone
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
            if packet and packet["type"] == "status" and status_style == "tab-scoped":
                # Additional valid delta form; "shared" replays the unchanged packets.
                packet = {key: value for key, value in packet.items() if key != "sessions"}
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
            assert capture.directory == folder and capture.microphone is microphone
            if not capture.finished:
                assert browser.states[2]["captureId"] == "reload-capture", step
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
        stops = [step for step in case["steps"] if any(a.startswith("stop:") for a in step["expect"])]
        ends = [event for event in capture.host.session.manifest["events"] if event["type"] == "end"]
        assert len(ends) == len(stops)
        if stops:
            assert ends[0]["reason"] == stops[0]["expect"][0].partition(":")[2]
        assert len(list((tmp_path / "Library").iterdir())) == 1
    finally:
        capture.stop()
