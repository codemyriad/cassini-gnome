# SPDX-License-Identifier: GPL-3.0-or-later
"""Returning browser PCM restores one span atomically; failure keeps system audio."""

import json

import pytest
from test_browser_capture import Tap

from cassini_talk.browser_capture import BrowserCapture
from cassini_talk.session_events import source_state


def recording(tmp_path):
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Tap)
    capture.start(dict(type="start", captureId="retained", timelineOriginMs=1, microphone=True))
    capture.elapsed_ms = lambda: 1000
    capture.handle(dict(type="track", id="original", kind="remote", sampleRate=48000, channels=1))
    capture.handle(dict(type="audio", id="original", sequence=0, offsetMs=0, pcm="AQACAA=="))
    capture.fallback()
    return capture


def test_same_wire_source_restores_new_wav_and_keeps_microphone_and_folder(tmp_path):
    capture = recording(tmp_path)
    try:
        microphone, folder = capture.microphone, capture.directory
        old = capture.host.session.manifest["tracks"][1]
        assert old["ended"] and not capture.browser_ready
        system = capture.native_taps["system"][1]
        # A declaration from an obsolete document is never sufficient.
        capture.handle(dict(type="track", id="obsolete", kind="remote", sampleRate=48000, channels=1))
        capture.restore()
        assert capture.browser_lost and not system.stopped
        capture.handle(dict(type="audio", id="original", sequence=1, offsetMs=1100, pcm="AQACAA=="))
        assert capture.browser_ready
        capture.restore()
        assert not capture.browser_lost and system.stopped
        assert capture.microphone is microphone and capture.directory == folder
        assert source_state(capture.host.session.manifest["events"]).reason == "restored"
        spans = [t for t in capture.host.session.manifest["tracks"] if t.get("sourceTrackId") == "original"]
        assert len(spans) == 2 and spans[0]["file"] != spans[1]["file"]
        assert spans[1]["startOffsetMs"] >= spans[0]["endOffsetMs"]
        assert not any(t.get("sourceTrackId") == "obsolete" for t in capture.host.session.manifest["tracks"])
        capture.handle(dict(type="audio", id="original", sequence=2, offsetMs=1200, pcm="AQACAA=="))
        assert spans[1]["frames"] > 0
        capture.stop(reason="tabClosed")
        saved = json.loads((folder / "session.json").read_text())
        assert saved["events"][-1]["reason"] == "tabClosed"
    finally:
        capture.stop()


@pytest.mark.parametrize("failure", ["new-span", "sync"])
def test_failed_restore_keeps_current_selection_system_pcm_and_no_restored_event(tmp_path, monkeypatch, failure):
    capture = recording(tmp_path)
    capture.handle(dict(type="audio", id="original", sequence=1, offsetMs=1100, pcm="AQACAA=="))
    system = capture.native_taps["system"][1]
    before = (capture.directory / "session.json").read_bytes()
    events = list(capture.host.session.manifest["events"])
    files = {p.name for p in capture.directory.iterdir()}
    target, method = (
        (capture.streams, "_record") if failure == "new-span" else (capture.host.session.writers["original"], "sync")
    )
    original = getattr(target, method)

    def fail(*args, **kwargs):
        raise OSError("restore refused")

    monkeypatch.setattr(target, method, fail)
    try:
        with pytest.raises(OSError, match="refused"):
            capture.restore()
        assert capture.browser_lost and not capture.finished and not system.stopped
        assert (capture.me, capture.others) == (True, True)
        assert capture.host.session.manifest["events"] == events
        assert (capture.directory / "session.json").read_bytes() == before
        assert {p.name for p in capture.directory.iterdir()} == files
        monkeypatch.setattr(target, method, original)
        system.append(b"\x01\x00" * 480, 1200)
        capture.restore()
        assert not capture.browser_lost and system.stopped
    finally:
        monkeypatch.setattr(target, method, original)
        capture.stop()


def test_dead_audio_port_and_others_off_never_replace_system_with_empty_browser(tmp_path):
    capture = recording(tmp_path)
    try:
        capture.handle(dict(type="audio", id="original", sequence=1, offsetMs=1100, pcm="AQACAA=="))
        capture.audio_connected = False
        capture.restore()
        assert capture.browser_lost
        capture.audio_connected = True
        capture.select(True, False)
        capture.restore()
        assert capture.browser_lost and not capture.others
        assert source_state(capture.host.session.manifest["events"]).source == "none"
    finally:
        capture.stop()


@pytest.mark.parametrize("during_readiness", [False, True])
def test_closed_tab_cancels_pending_fallback_without_a_system_event_or_span(tmp_path, during_readiness):
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Tap)
    capture.start(dict(type="start", captureId="closing", timelineOriginMs=1, microphone=True))
    before = (capture.directory / "session.json").read_bytes()
    microphone = capture.microphone

    class Closing(Tap):
        def start(self):
            super().start()
            capture.tab_closed = True

    if during_readiness:
        capture.tap_factory = Closing
    else:
        capture.tab_closed = True
    try:
        capture.fallback()
        assert (capture.directory / "session.json").read_bytes() == before
        assert not capture.browser_lost and not capture.finished
        assert set(capture.native_taps) == {"microphone"}
        assert capture.microphone is microphone and not microphone.stopped
        if during_readiness:
            assert Tap.instances[-1].kind == "system" and Tap.instances[-1].stopped
        capture.stop(reason="tabClosed")
        assert capture.host.session.manifest["events"][-1]["reason"] == "tabClosed"
    finally:
        capture.stop()
