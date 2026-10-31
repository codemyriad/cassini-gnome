# SPDX-License-Identifier: GPL-3.0-or-later
"""Tab-scoped deltas retain capture ownership and explicit stopping state."""

import pytest
from test_browser_capture import Tap
from test_call_lifecycle import Inline

from cassini_viewer.browser_capture import BrowserCapture
from cassini_viewer.browser_service import BrowserConnection, BrowserService


@pytest.fixture
def owned(tmp_path):
    broker = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    broker.workers.shutdown()
    broker.workers = Inline()
    capture = BrowserCapture(broker._capture_changed, parent=tmp_path / "Library", tap_factory=Tap)
    capture.start(dict(type="start", captureId="owned", timelineOriginMs=1, microphone=True))
    broker.captures["owned"] = capture
    browser = BrowserConnection(None)
    browser.states = {3: dict(tabId=3, recording=True, captureId="owned", stopping=True, path=str(capture.directory))}
    broker.browsers[browser.id] = browser
    try:
        yield broker, browser, capture
    finally:
        capture.stop()


@pytest.mark.parametrize("instance", [None, "fixture:99"])
@pytest.mark.parametrize("stopping", [None, False, True])
def test_tab_delta_preserves_owned_identity_and_omitted_stopping(owned, instance, stopping):
    broker, browser, capture = owned
    browser.browser_instance = instance
    if instance is not None:
        broker.call_captures[(instance, 3)] = ("owned", "https://meeting.example/call")
    packet = dict(recording=True, tabId=3, pageState="present")
    if stopping is not None:
        packet["stopping"] = stopping
    broker._status(browser, packet)
    state = browser.states[3]
    assert state["captureId"] == "owned"
    assert state["path"] == str(capture.directory)
    assert state["stopping"] is (True if stopping is None else stopping)


def test_first_tab_scoped_announcement_keeps_explicit_identity_before_audio(tmp_path):
    broker = BrowserService(tmp_path / "settings.json", notifier=lambda *_: None)
    browser = BrowserConnection(None, browser_instance="fixture:99")
    try:
        broker._status(browser, dict(recording=True, tabId=3, captureId="early", stopping=True, path="/private/early"))
        assert browser.status["captureId"] == browser.states[3]["captureId"] == "early"
        assert browser.status["stopping"] is browser.states[3]["stopping"] is True
        assert broker.call_captures[("fixture:99", 3)][0] == "early"
        broker._status(browser, dict(recording=True, tabId=3, pageState="present"))
        assert browser.states[3]["captureId"] == "early"
        assert browser.states[3]["stopping"] is True
    finally:
        broker.workers.shutdown()


def test_new_capture_does_not_inherit_previous_stopping_or_path(owned):
    broker, browser, _capture = owned
    broker._status(browser, dict(recording=True, tabId=3, captureId="replacement", stopping=False))
    assert browser.states[3]["captureId"] == "replacement"
    assert browser.states[3]["stopping"] is False
    assert "path" not in browser.states[3]


@pytest.mark.parametrize("instance", [None, "fixture:99"])
def test_full_inventory_can_clear_omitted_stopping_without_losing_identity(owned, instance):
    broker, browser, capture = owned
    browser.browser_instance = instance
    if instance is not None:
        broker.call_captures[(instance, 3)] = ("owned", "https://meeting.example/call")
    broker._status(browser, dict(recording=True, sessions=[dict(tabId=3, recording=True, captureId="owned")]))
    assert browser.states[3]["captureId"] == "owned"
    assert browser.states[3]["path"] == str(capture.directory)
    assert "stopping" not in browser.states[3]
