# SPDX-License-Identifier: GPL-3.0-or-later
"""Browser loss diagnostics survive fallback without becoming Stop policy."""

import json

import pytest
from test_browser_capture import remote, server, start, stream  # noqa: F401

from cassini_talk.browser_audio_proxy import BrowserAudioProxy
from cassini_talk.session_events import read_events


def loss_event(capture):
    events = json.loads((capture.directory / "session.json").read_text())["events"]
    return next(event for event in events if event["type"] == "othersSource" and event["reason"] == "lost")


@pytest.mark.parametrize(
    "cause",
    [
        "frameError",
        "frameClosed",
        "tabClosed",
        "nativeHostError",
        "startupError",
        "transportClosed",
        "lifecycle",
        "futureLoss",
        "vendor.future:loss-v2",
        None,
    ],
)
def test_lost_stop_preserves_first_cause_and_detail_without_ending_recording(server, cause):  # noqa: F811
    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    detail = "Generated terminal browser loss."
    try:
        start(client, "generated-loss", microphone=False)
        remote(client, "wire")
        capture = service.captures["generated-loss"]
        message = dict(type="stop", failureReason="lost", problem=detail)
        if cause is not None:
            message["cause"] = cause
        assert client.handle(message)["type"] == "stopped"
        first = loss_event(capture)
        assert first["source"] == "system" and first["detail"] == detail
        assert first.get("cause") == cause
        assert capture.browser_lost and not capture.finished and not capture.stop_requested.is_set()
        # Cleanup after a delivered loss is not a new terminal cause.
        capture.close("Generated later transport cleanup.")
        assert loss_event(capture) == first
        stream(capture, "system")
        client.handle(dict(type="stop"))
        saved = json.loads((capture.directory / "session.json").read_text())
        assert saved["state"] == "completed" and "problem" not in saved
        assert saved["events"][-1]["reason"] == "user"
        assert read_events(saved["events"]) == tuple(saved["events"])
    finally:
        client.close()


def test_audio_transport_eof_records_its_observed_cause(server):  # noqa: F811
    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    start(client, "generated-eof", microphone=False)
    remote(client, "wire")
    capture = service.captures["generated-eof"]
    client.close()
    service.changes.wait(lambda: capture.browser_lost, "EOF fallback was not committed")
    event = loss_event(capture)
    assert event["cause"] == "transportClosed" and event["detail"]
    assert not capture.finished
    stream(capture, "system")
    capture.stop()


@pytest.mark.parametrize("cause", ["", "x" * 81, "not an identifier", "non-ascii-\u00e9", ["frameError"]])
def test_malformed_diagnostic_cause_does_not_change_loss_policy(server, cause):  # noqa: F811
    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "generated-bad-cause", microphone=False)
        remote(client, "wire")
        capture = service.captures["generated-bad-cause"]
        assert (
            client.handle(dict(type="stop", failureReason="lost", cause=cause, problem="x" * 2000))["type"] == "stopped"
        )
        event = loss_event(capture)
        assert "cause" not in event and event["detail"] == "x" * 1000
        assert capture.browser_lost and not capture.finished
    finally:
        client.close()


def test_explicit_stop_does_not_add_loss_diagnostics(server):  # noqa: F811
    service, path, _root = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "generated-user-stop", microphone=False)
        remote(client, "wire")
        capture = service.captures["generated-user-stop"]
        client.handle(dict(type="stop", cause="frameError"))
        events = json.loads((capture.directory / "session.json").read_text())["events"]
        assert events[-1]["type"] == "end" and events[-1]["reason"] == "user"
        assert all("cause" not in event and "detail" not in event for event in events)
    finally:
        client.close()
