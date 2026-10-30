# SPDX-License-Identifier: GPL-3.0-or-later
"""Rejected source transitions retain the confirmed capture and release devices."""

import json
import wave

import pytest
from test_browser_capture import Tap, remote, server, start  # noqa: F401

from cassini_viewer.browser_audio_proxy import BrowserAudioProxy
from cassini_viewer.browser_service import request


def test_fallback_uses_input_host_stamps_and_keeps_other_sessions_independent(server):  # noqa: F811
    service, path, _ = server
    one, two = BrowserAudioProxy(path=path), BrowserAudioProxy(path=path)
    try:
        start(one, "host-gap", microphone=False)
        start(two, "unaffected", microphone=False)
        remote(two, "remote-unaffected")
        capture = service.captures["host-gap"]
        capture.fallback()
        identifier, tap = capture.native_taps["system"]
        track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
        offset = track["startOffsetMs"] + 100
        # The callback offset deliberately stays fixed. Only consecutive input
        # host stamps may decide whether a buffer represents missing audio.
        tap.append(b"\1\0" * 48000, offset, host_ns=10_000_000_000)
        tap.append(b"\2\0" * 48000, offset, host_ns=12_000_000_000)
        tap.append(b"\3\0" * 48000, offset, host_ns=13_000_050_000)
        capture.stop()
        assert capture.host.session.manifest["problemKind"] == "gap"
        assert track["gapFrames"] == 48000 and track["frames"] == 4800 + 4 * 48000
        with wave.open(str(capture.directory / track["file"])) as audio:
            assert audio.readframes(4800) == bytes(9600)
            assert audio.readframes(48000) == b"\1\0" * 48000
            assert audio.readframes(48000) == bytes(96000)
            assert audio.readframes(48000) == b"\2\0" * 48000
            assert audio.readframes(48000) == b"\3\0" * 48000
        assert not service.captures["unaffected"].finished
        two.handle(dict(type="stop"))
        assert service.captures["unaffected"].host.session.manifest["state"] == "completed"
    finally:
        one.close()
        two.close()


@pytest.mark.parametrize("via_callback", [False, True])
def test_failed_replacement_keeps_old_source_and_manifest(server, via_callback):  # noqa: F811
    service, path, _ = server
    client = BrowserAudioProxy(path=path)

    class Broken(Tap):
        def __init__(self, kind, origin, append, failed, **kwargs):
            super().__init__(kind, origin, append, failed, **kwargs)
            self.failed = failed

        def start(self):
            super().start()
            if via_callback:
                self.failed("Device unavailable")
            else:
                raise OSError("Device unavailable")

    try:
        start(client, "change-failure", microphone=False)
        remote(client, "remote")
        capture = service.captures["change-failure"]
        capture.fallback()
        old = capture.native_taps["system"][1]
        before = (capture.directory / "session.json").read_bytes()
        files = {p.name for p in capture.directory.iterdir()}
        capture.tap_factory = Broken
        with pytest.raises(ValueError, match="Device unavailable"):
            request("selection", path=path, directory=str(capture.directory), me=True, others=False)
        assert not capture.finished and not capture.error
        assert (capture.me, capture.others, capture.browser_lost) == (False, True, True)
        assert capture.native_taps == {"system": (capture.native_taps["system"][0], old)}
        assert not old.stopped and Tap.instances[-1].stopped
        assert (capture.directory / "session.json").read_bytes() == before
        assert {p.name for p in capture.directory.iterdir()} == files
        old.append(b"\x01\x00" * 480, capture.elapsed_ms() + 100)
        assert not capture.finished
        capture.stop()
    finally:
        client.close()


@pytest.mark.parametrize("limit", ["tracks", "events"])
def test_browser_close_failure_finalizes_and_releases_microphone(server, limit):  # noqa: F811
    from test_browser_service import wait_for

    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    start(client, "close-limit")
    capture = service.captures["close-limit"]
    with capture.lock:
        if limit == "tracks":
            for index in range(63):
                capture.host.session.add_track(f"ended-{index}", "Old", "remote", 48000, 1)
                capture.host.session.end_track(f"ended-{index}")
        else:
            from cassini_viewer.session_events import append_events

            append_events(
                capture.host.session.manifest,
                [dict(atMs=0, type="selection", me=True, others=True, reason="user")] * 997,
            )
            capture.host.session.checkpoint()
    client.close()
    wait_for(lambda: capture.finished)
    assert capture.microphone.stopped
    assert all(tap.stopped for tap in Tap.instances)
    assert not capture.stopping and capture.stopped.is_set()
    assert json.loads((capture.directory / "session.json").read_text())["state"] == "interrupted"


def test_me_only_browser_loss_closes_local_span_and_publishes_activity(server):  # noqa: F811
    service, path, _ = server
    client = BrowserAudioProxy(path=path)
    try:
        start(client, "me-only")
        capture = service.captures["me-only"]
        # Legacy per-page local spans can be present alongside the native mic.
        client.handle(dict(type="track", id="local", label="Page mic", kind="local", sampleRate=48000, channels=1))
        capture.select(True, False)
        events = list(capture.host.session.manifest["events"])
        changed = []
        original = capture.changed
        capture.changed = lambda c, event: (changed.append(event), original(c, event))
        capture.fallback()
        assert capture.browser_lost and not capture.streams.browser
        assert capture.streams.tracks["local"].recorded is None
        assert next(t for t in capture.host.session.manifest["tracks"] if t["id"] == "local")["ended"]
        assert capture.host.session.manifest["events"] == events  # No invented others event.
        assert changed == ["sources"]
        assert set(capture.native_taps) == {"microphone"} and not capture.microphone.stopped
        capture.stop()
    finally:
        client.close()


def test_retiring_source_error_cannot_stop_its_confirmed_replacement(server):  # noqa: F811
    service, path, _ = server
    client = BrowserAudioProxy(path=path)

    class Retiring(Tap):
        def __init__(self, kind, origin, append, failed, **kwargs):
            super().__init__(kind, origin, append, failed, **kwargs)
            self.failed = failed

        def stop(self):
            super().stop()
            self.failed("Old device closed")

    try:
        start(client, "retiring-source", microphone=False)
        capture = service.captures["retiring-source"]
        capture.tap_factory = Retiring
        capture.fallback()
        events = []
        original = capture.changed
        capture.changed = lambda c, event: (events.append(event), original(c, event))
        old = capture.native_taps["system"][1]
        capture.select(True, False)
        assert old.stopped and "error" not in events
        assert not capture.error and not capture.finished and not capture.microphone.stopped
        assert (capture.me, capture.others) == (True, False)
        capture.stop()
    finally:
        client.close()
