# SPDX-License-Identifier: GPL-3.0-or-later
"""Run shared literal D33 actions through the real broker and session writer."""

import json
import os
import time
from concurrent.futures import Future
from pathlib import Path

import pytest
from test_browser_capture import Tap

from cassini_viewer.browser_capture import BrowserCapture
from cassini_viewer.browser_service import BrowserConnection, BrowserService
from cassini_viewer.call_lifecycle import BROWSER_RETURN_GRACE, CALL_END_GRACE

VECTORS = Path(
    os.environ.get(
        "CASSINI_TEST_CALL_VECTORS",
        Path(__file__).resolve().parents[1] / "browser-protocol/conformance/control/call-lifecycle-vectors.json",
    )
)


class Inline:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
        return future


@pytest.mark.parametrize("case", json.loads(VECTORS.read_text())["cases"], ids=lambda case: case["name"])
def test_shared_actions_use_actual_capture_devices_spans_and_end(tmp_path, monkeypatch, case):
    assert json.loads(VECTORS.read_text())["constants"] == dict(
        CALL_END_GRACE=CALL_END_GRACE,
        BROWSER_RETURN_GRACE=BROWSER_RETURN_GRACE,
    )
    now = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    service = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    service.workers.shutdown()
    service.workers = Inline()
    capture = BrowserCapture(service._capture_changed, parent=tmp_path / "Library", tap_factory=Tap)
    capture.start(dict(type="start", captureId="call", timelineOriginMs=int(time.time() * 1000), microphone=True))
    capture.elapsed_ms = lambda: now[0] * 1000
    capture.host.handle(dict(type="track", id="original", kind="remote", sampleRate=48000, channels=1))
    capture.host.handle(dict(type="audio", id="original", sequence=0, offsetMs=0, pcm="AQACAA=="))
    if not case.get("others", True):
        capture.select(True, False)
    service.captures["call"] = capture
    browser = BrowserConnection(None)
    browser.capabilities = {"independent-sessions-v1", "page-state-v1"}
    if case.get("callEvidence"):
        browser.capabilities.add("call-evidence-v1")
    browser.status_seen = True
    browser.states = {3: dict(recording=True, tabId=3, captureId="call")}
    service.browsers[browser.id] = browser
    commands, actions = [], []
    monkeypatch.setattr(service, "command", lambda *args, **kwargs: commands.append((args, kwargs)))
    lifecycle = service._capture_lifecycle

    def record(*args):
        actions.append(args[-1])
        return lifecycle(*args)

    monkeypatch.setattr(service, "_capture_lifecycle", record)
    folder, microphone = capture.directory, capture.microphone
    try:
        for index, step in enumerate(case["steps"]):
            now[0] = step["atMs"] / 1000
            same = step.get("sameTab", True)
            page = step["pageState"] if same else None
            count = step["sourceCount"] if same else 0
            browser.states[3]["pageState"] = page
            browser.meetings = {3: dict(pageState=page, sourceCount=count, heard=step.get("heard", True))}
            # Only real PCM on the retained transport makes restore ready.
            if not capture.finished and count and capture.browser_lost and step.get("restoreReady", True):
                wire = "return-" + str(index)
                capture.handle(dict(type="track", id=wire, kind="remote", sampleRate=48000, channels=1))
                capture.handle(dict(type="audio", id=wire, sequence=0, offsetMs=step["atMs"], pcm="AQACAA=="))
            actions.clear()
            service.policy()
            assert actions == step["expect"], (case["name"], step, capture.error)
            assert capture.directory == folder and capture.microphone is microphone
            if not capture.finished:
                assert not any(e["type"] == "end" for e in capture.host.session.manifest["events"])
        ends = [e for e in capture.host.session.manifest["events"] if e["type"] == "end"]
        stops = [s for s in case["steps"] if s["expect"] and s["expect"][0].startswith("stop:")]
        assert len(ends) == len(stops)
        if stops:
            assert ends[0]["reason"] == stops[0]["expect"][0].split(":")[1]
        if case["name"] == "closed-tab-never-starts-computer-sound":
            assert not any(t["kind"] == "system" for t in capture.host.session.manifest["tracks"])
        if any(s["expect"] == ["restore"] for s in case["steps"]):
            restored = [e for e in capture.host.session.manifest["events"] if e.get("reason") == "restored"]
            assert len(restored) == 1
            assert capture.host.session.manifest["tracks"][0]["kind"] == "microphone"
        assert len(list((tmp_path / "Library").iterdir())) == 1
        assert all(args[1] == "stop" for args, _kwargs in commands)
    finally:
        capture.stop()


def test_returning_prejoin_receive_slots_do_not_reset_the_deadline(tmp_path, monkeypatch):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="return-prejoin",
            callEvidence=True,
            steps=[
                dict(atMs=0, pageState="navigating", sourceCount=0, expect=[]),
                dict(atMs=2000, pageState="navigating", sourceCount=0, expect=["fallback"]),
                dict(atMs=119999, pageState="present", sourceCount=3, heard=False, restoreReady=False, expect=[]),
                dict(
                    atMs=120000,
                    pageState="present",
                    sourceCount=3,
                    heard=False,
                    restoreReady=False,
                    expect=["stop:pageGone"],
                ),
            ],
        ),
    )


def test_hangup_after_failed_restore_keeps_the_normal_call_end_grace(tmp_path, monkeypatch):
    test_shared_actions_use_actual_capture_devices_spans_and_end(
        tmp_path,
        monkeypatch,
        dict(
            name="hangup-after-restore-failure",
            steps=[
                dict(atMs=0, pageState="navigating", sourceCount=0, expect=[]),
                dict(atMs=2000, pageState="navigating", sourceCount=0, expect=["fallback"]),
                dict(atMs=60000, pageState="present", sourceCount=2, restoreReady=False, expect=[]),
                dict(atMs=61000, pageState="present", sourceCount=0, expect=[]),
                dict(atMs=63000, pageState="present", sourceCount=0, expect=["stop:callEnded"]),
            ],
        ),
    )
