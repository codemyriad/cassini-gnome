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


@pytest.mark.parametrize("stop_clock,expected_end", [(0.3, 510), (0.568, 568)])
def test_off_side_discards_pcm_but_checks_sequence_geometry_and_new_span_offsets(tmp_path, stop_clock, expected_end):
    now = [0.0]
    host = BrowserHost(tmp_path, clock=lambda: now[0])
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
    now[0] = stop_clock
    host.handle(dict(type="stop"))
    assert resumed["frames"] == 480 and resumed["endOffsetMs"] == 510
    assert (session.directory / resumed["file"]).read_bytes()[44:] == b"\x03\x00" * 480
    assert (session.directory / old["file"]).read_bytes() == before
    saved = json.loads((session.directory / "session.json").read_text())
    assert saved["events"][-2]["atMs"] == resumed["startOffsetMs"]
    assert saved["events"][-1] == dict(type="end", atMs=pytest.approx(expected_end), reason="user")


def test_fallback_never_switches_a_browser_source_back_or_reuses_its_wav(tmp_path):
    host = BrowserHost(tmp_path, clock=lambda: 0.0)
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


@pytest.mark.parametrize("use_selection", [False, True])
def test_browser_root_identity_survives_wire_spans_and_selection(tmp_path, use_selection):
    """Receiver identity, not a transient span or a label, defines the speaker."""
    from cassini_talk.library import LibraryScanner
    from cassini_talk.transcription import prepare_tracks

    host = BrowserHost(tmp_path, clock=lambda: 0.0)
    host.handle(dict(type="start"))
    session = host.session
    if use_selection:
        host.audio_sink = BrowserStreams(session)
    for index, identity in enumerate(("receiver-a", "receiver-a", "receiver-b")):
        identifier = f"wire-span-{index}"
        host.handle(
            dict(
                type="track",
                id=identifier,
                label="Same label",
                kind="remote",
                sampleRate=48000,
                channels=1,
                sourceTrackId=identity,
            )
        )
        host.handle(
            dict(
                type="audio",
                id=identifier,
                sequence=0,
                offsetMs=index * 1000,
                pcm=base64.b64encode(b"\x01\x00" * 4800).decode(),
            )
        )
    if use_selection:
        with session.change([]):
            host.audio_sink.select(True, False, 3000)
        with session.change([]):
            host.audio_sink.select(True, True, 4000)
        host.handle(
            dict(
                type="audio",
                id="wire-span-0",
                sequence=1,
                offsetMs=4000,
                pcm=base64.b64encode(b"\x02\x00" * 4800).decode(),
            )
        )
    host.handle(dict(type="stop"))
    saved = json.loads((session.directory / "session.json").read_text())
    assert [t["sourceTrackId"] for t in saved["tracks"][:3]] == ["receiver-a", "receiver-a", "receiver-b"]
    if use_selection:
        resumed = saved["tracks"][3]
        assert resumed["id"] != "wire-span-0" and resumed["sourceTrackId"] == "receiver-a"
        assert resumed["startOffsetMs"] == 4000 and resumed["frames"] == 4800
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    tracks, speakers = prepare_tracks(meeting)
    assert len(speakers) == 2
    assert tracks[0]["speaker"] == tracks[1]["speaker"] != tracks[2]["speaker"]
    assert all(s["name"] is None for s in speakers.values())


@pytest.mark.parametrize("identity", [None, "receiver", "é" * 100])
@pytest.mark.parametrize("use_selection", [False, True])
def test_browser_source_identity_accepts_null_or_utf8_bound(tmp_path, identity, use_selection):
    host = BrowserHost(tmp_path, clock=lambda: 0.0)
    host.handle(dict(type="start"))
    if use_selection:
        host.audio_sink = BrowserStreams(host.session)
    host.handle(dict(type="track", id="span", kind="remote", sampleRate=48000, channels=1, sourceTrackId=identity))
    track = host.session.manifest["tracks"][0]
    assert track.get("sourceTrackId") == (identity or ("span" if use_selection else None))
    host.handle(dict(type="stop"))


@pytest.mark.parametrize("identity", ["", "é" * 101, 7, [], {}])
@pytest.mark.parametrize("use_selection", [False, True])
def test_invalid_browser_identity_allocates_no_track(tmp_path, identity, use_selection):
    host = BrowserHost(tmp_path, clock=lambda: 0.0)
    host.handle(dict(type="start"))
    if use_selection:
        host.audio_sink = BrowserStreams(host.session)
    before = (host.session.directory / "session.json").read_bytes()
    with pytest.raises(ProtocolError, match="source track identity"):
        host.handle(dict(type="track", id="span", kind="remote", sampleRate=48000, channels=1, sourceTrackId=identity))
    assert not host.session.manifest["tracks"] and not host.session.writers
    assert not host.sequences and not list(host.session.directory.glob("*.wav"))
    assert (host.session.directory / "session.json").read_bytes() == before
    host.handle(dict(type="stop"))


@pytest.mark.parametrize(
    "end,expected",
    [
        (1028.784049, 240),  # Main CI run 42254121515: float subtraction added one frame.
        (1028.784048, 240),  # One ns before the exact sample boundary.
        (1028.784050, 241),  # One ns after it: a genuinely partial frame is kept.
        (1023.784049, 0),
        (1023.784048, 0),
        (1023.784050, 1),
        (1043.784049, 480),
    ],
)
@pytest.mark.parametrize("channels", [1, 2])
def test_native_stop_cut_uses_nanosecond_stamps_without_an_extra_frame(end, expected, channels):
    from cassini_talk.capture_sources import pcm_before, selected_pcm

    pcm = b"".join(frame.to_bytes(2, "little") * channels for frame in range(480))
    before = pcm_before(pcm, 1023.784049, 48000, channels, end)
    after, _offset = selected_pcm(pcm, 1023.784049, 48000, channels, end)
    assert before == pcm[: expected * channels * 2]
    assert len(before) + len(after) == len(pcm)
    assert before + after == pcm


@pytest.mark.parametrize("rate", [8000, 44100, 48000, 96000, 192000])
@pytest.mark.parametrize("offset", [0, 83.784049, 1023.784049, 1234567.000001])
@pytest.mark.parametrize("frames", [1, 2, 4, 5, 481, 482])
@pytest.mark.parametrize("channels", [1, 2])
def test_sample_derived_track_end_partitions_pcm_exactly(rate, offset, frames, channels):
    from cassini_talk.capture_sources import pcm_before, selected_pcm, track_end

    pcm = b"".join(frame.to_bytes(2, "little") * channels for frame in range(960))
    boundary = track_end(dict(startOffsetMs=offset, frames=frames, sampleRate=rate))
    before = pcm_before(pcm, offset, rate, channels, boundary)
    after, selected_offset = selected_pcm(pcm, offset, rate, channels, boundary)
    assert before == pcm[: frames * channels * 2]
    assert after == pcm[frames * channels * 2 :]
    assert len(before) + len(after) == len(pcm) and before + after == pcm
    assert selected_offset == boundary


@pytest.mark.parametrize("rate", [8000, 44100, 48000, 96000, 192000])
@pytest.mark.parametrize("offset", [0, 83.784049, 1000.0, 1023.784049])
@pytest.mark.parametrize("skip", [1, 2, 4, 5])
@pytest.mark.parametrize("stop_frame", [480, 481, 482])
def test_selection_then_stop_does_not_requantize_fractional_sample_timestamps(rate, offset, skip, stop_frame):
    from cassini_talk.capture_sources import pcm_before, selected_pcm, track_end

    pcm = b"".join(frame.to_bytes(2, "little") for frame in range(960))
    # Select halfway into the preceding frame: skip exactly `skip` frames.
    start = offset + (skip - 0.5) * 1000 / rate
    selected, selected_offset = selected_pcm(pcm, offset, rate, 1, start)
    assert selected == pcm[skip * 2 :]
    end = track_end(dict(startOffsetMs=offset, frames=stop_frame, sampleRate=rate))
    kept = pcm_before(selected, selected_offset, rate, 1, end)
    assert kept == pcm[skip * 2 : stop_frame * 2]
    assert len(kept) // 2 == stop_frame - skip
