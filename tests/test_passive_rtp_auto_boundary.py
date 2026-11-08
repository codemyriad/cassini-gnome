# SPDX-License-Identifier: GPL-3.0-or-later
"""D80.1: passive source availability must not become a device-opening automatic Start.

The .25 worker VM projects the actual Chromium passive-viewer discovery into this
neutral-origin legacy row. Exercise production policy/dispatch, not just a mocked
command method. Only the outbound wire sink is fake; it ACKs no recording.
"""

import io

import pytest

from cassini_talk.browser_service import BrowserConnection, BrowserService

CAPABILITIES = [
    "independent-sessions-v1",
    "call-evidence-v1",
    "page-state-v1",
    "multi-stream-v1",
    "resume-capture-v1",
    "heartbeat-v1",
    "tab-readiness-v1",
    "call-signals-v1",
]


def boundary(tmp_path, monkeypatch, *, automatic):
    def forbidden(*_args, **_kwargs):
        pytest.fail("This metadata-only regression must never open native fallback/input")

    service = BrowserService(
        tmp_path / "settings.json",
        notifier=lambda *_: None,
        capture_parent=tmp_path / "library",
        tap_factory=forbidden,
    )
    browser = BrowserConnection(io.BytesIO(), browser_instance="passive-test-profile")
    service.browsers[browser.id] = browser
    queued, packets = [], []
    monkeypatch.setattr(service.workers, "submit", lambda function, *args: queued.append((function, args)))
    monkeypatch.setattr(service, "start_fallback", forbidden)

    def send(packet):
        packets.append(packet)
        if packet["type"] == "configure":
            service.update(browser, dict(type="configured", capabilities=packet["capabilities"]))
        elif packet["type"] == "command":
            service.update(
                browser,
                dict(
                    type="result",
                    requestId=packet["requestId"],
                    ok=True,
                    status=dict(recording=False),
                ),
            )

    monkeypatch.setattr(browser, "send", send)
    service.require_application = True
    service.applications["isolated-test-application"] = True
    service.auto_record = automatic
    try:
        service.update(browser, dict(type="hello", version=1, extensionVersion="0.2.25", capabilities=CAPABILITIES))
        service.update(browser, dict(type="status", recording=False))
        service.update(
            browser,
            dict(
                type="meetings",
                meetings=[
                    dict(
                        tabId=2,
                        title="Passive audio viewer",
                        url="https://media.invalid/viewer",
                        sourceCount=1,
                        sourceCountKnown=True,
                        heard=True,
                        callGeneration="passive-3",
                        pageState="present",
                    )
                ],
            ),
        )
        while queued:
            function, arguments = queued.pop(0)
            function(*arguments)  # Real _automatic -> command -> BrowserConnection.send.
        observed = service.snapshot()["meetings"][0]
        assert observed["sourceCount"] == 1 and observed["heard"] is True
        assert browser.signals_configured, "The .25 call-signals handshake must not be omitted"
        assert not service.captures
        return [packet for packet in packets if packet["type"] == "command"]
    finally:
        service.workers.shutdown(wait=True, cancel_futures=True)


def test_passive_rtp_with_auto_record_off_sends_no_start(tmp_path, monkeypatch):
    assert boundary(tmp_path, monkeypatch, automatic=False) == []


def test_passive_rtp_must_not_cross_automatic_start_boundary(tmp_path, monkeypatch):
    packets = boundary(tmp_path, monkeypatch, automatic=True)
    assert packets == [], f"Passive playback crossed automatic Start boundary: {packets}"
