# SPDX-License-Identifier: GPL-3.0-or-later
"""Changing a side never resumes an ended WAV or skips wire validation."""

import base64
import json

import pytest

from cassini_talk.capture_sources import BrowserStreams, change_time
from cassini_talk.recording_session import BrowserHost, ProtocolError


def audio(host, sequence, offset, pcm):
    return host.handle(
        dict(type="audio", id="remote", sequence=sequence, offsetMs=offset, pcm=base64.b64encode(pcm).decode())
    )


def test_off_side_discards_pcm_but_checks_sequence_geometry_and_new_span_offsets(tmp_path):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start"))
    session = host.session
    streams = host.audio_sink = BrowserStreams(session)
    host.handle(dict(type="track", id="remote", label="Other", kind="remote", sampleRate=48000, channels=1))
    audio(host, 0, 0, b"\x01\x00" * 4800)
    old = session.manifest["tracks"][0]
    with session.change(
        [
            dict(type="selection", me=True, others=False, reason="user", atMs=100),
            dict(type="othersSource", source="none", reason="user", atMs=100),
        ]
    ):
        streams.select(True, False, 100)
    before = (session.directory / old["file"]).read_bytes()
    assert old["ended"] and old["endOffsetMs"] == 100
    with pytest.raises(ProtocolError, match="order"):
        audio(host, 2, 100, b"\x02\x00" * 4800)
    with pytest.raises(ProtocolError, match="Incomplete PCM frame"):
        audio(host, 1, 100, b"x")
    with pytest.raises(ProtocolError, match="out of order"):
        audio(host, 1, 0, b"\x02\x00" * 4800)
    audio(host, 1, 100, b"\x02\x00" * 4800)
    assert len(session.manifest["tracks"]) == 1
    assert (session.directory / old["file"]).read_bytes() == before
    with session.change(
        [
            dict(type="selection", me=True, others=True, reason="user", atMs=500),
            dict(type="othersSource", source="browser", reason="user", atMs=500),
        ]
    ):
        streams.select(True, True, 500)
    resumed = session.manifest["tracks"][1]
    assert resumed["startOffsetMs"] == 500 and resumed["id"] != old["id"] and resumed["file"] != old["file"]
    assert old["sourceTrackId"] == resumed["sourceTrackId"] == "remote"
    audio(host, 2, 490, b"\x02\x00" * 480 + b"\x03\x00" * 480)
    host.handle(dict(type="stop"))
    assert resumed["frames"] == 480 and resumed["endOffsetMs"] == 510
    assert (session.directory / resumed["file"]).read_bytes()[44:] == b"\x03\x00" * 480
    assert (session.directory / old["file"]).read_bytes() == before
    saved = json.loads((session.directory / "session.json").read_text())
    assert saved["events"][-2]["atMs"] == resumed["startOffsetMs"]
    assert saved["events"][-1] == dict(type="end", atMs=510, reason="user")


def test_fallback_never_switches_a_browser_source_back_or_reuses_its_wav(tmp_path):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start"))
    streams = host.audio_sink = BrowserStreams(host.session)
    host.handle(dict(type="track", id="remote", label="Other", kind="remote", sampleRate=48000, channels=1))
    audio(host, 0, 100, b"\x01\x00" * 4800)
    assert change_time(host.session, 150) == 200
    with host.session.change([dict(type="othersSource", source="system", reason="lost", atMs=200)]):
        streams.select(True, True, 200, browser=False)
        host.session.add_track("system", "The others", "system", 48000, 1)
        host.session.manifest["tracks"][-1]["startOffsetMs"] = 200
    audio(host, 1, 200, b"\x02\x00" * 4800)
    host.handle(dict(type="track", id="late", label="Reconnected", kind="remote", sampleRate=48000, channels=1))
    assert len(host.session.manifest["tracks"]) == 2
    assert host.session.manifest["tracks"][0]["frames"] == 4800
    host.session.append("system", b"\x03\x00" * 4800, 200)
    host.handle(dict(type="stop"))
