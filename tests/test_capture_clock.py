# SPDX-License-Identifier: GPL-3.0-or-later
"""Measured native host stamps, sparse over hours; no device/arrival-time guesses."""

import json
import wave

from cassini_talk.capture_clock import ClockAnchors
from cassini_talk.recording_session import RecordingSession


def test_first_minute_boundaries_and_latest_stamp_keep_an_hour_bounded():
    clock = ClockAnchors(48000, 1_000_000_000)
    for second in range(3601):
        clock.add(second * 48000, 1_000_000_000 + second * 1_000_050_000)
    points = clock.values()
    assert len(points) == 61
    assert points[0] == [0, 0] and points[-1] == [3600 * 48000, 3600 * 1_000_050_000]
    clock.add(3600 * 48000 + 960, 1_000_000_000 + 3600 * 1_000_050_000 + 20_001_000)
    points = clock.values()
    assert len(points) == 62  # One recent point in addition to minute observations.


def test_invalid_duplicate_and_backwards_stamps_never_replace_an_observation():
    clock = ClockAnchors(8000, 10_000_000)
    clock.add(0, 10_000_000)
    assert clock.values() == [[0, 0]]
    clock.add(8000, 1_010_000_000)
    assert clock.values() == [[0, 0], [8000, 1_000_000_000]]
    for index, stamp in [
        (8000, 2_010_000_000),
        (16000, 9_000_000),
        (-1, 3_000_000_000),
        (16000, 500_000_000),
        (True, 3_000_000_000),
        (16000, None),
    ]:
        clock.add(index, stamp)
        assert clock.values() == [[0, 0], [8000, 1_000_000_000]]


def session(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    capture = RecordingSession(None, source="gnome")
    capture._native_clock_origin_ns = 1_000_000_000
    capture.add_track("mic", "Me", "microphone", 8000, 1)
    return capture


def test_native_anchors_count_materialized_gap_samples_once_and_keep_pcm_exact(tmp_path, monkeypatch):
    capture = session(tmp_path, monkeypatch)
    first, last = b"\x10\x00" * 8000, b"\x20\x00" * 8000
    capture.append_native("mic", first, 0, host_ns=1_000_000_000)
    capture.append_native("mic", last, 2000, gap_frames=8000, host_ns=3_000_000_000)
    capture.finish()
    (track,) = capture.manifest["tracks"]
    assert track["clockAnchors"] == [[0, 0], [16000, 2_000_000_000]]
    assert track["frames"] == 24000 and track["gapFrames"] == 8000
    with wave.open(str(capture.directory / track["file"])) as source:
        assert source.readframes(24000) == first + bytes(16000) + last
    saved = json.loads((capture.directory / "session.json").read_text())
    assert saved["tracks"][0]["clockAnchors"] == track["clockAnchors"]


def test_first_actual_sample_index_includes_the_preexisting_selection_prefix(tmp_path, monkeypatch):
    capture = session(tmp_path, monkeypatch)
    capture.manifest["tracks"][0]["startOffsetMs"] = 0
    capture.append_native("mic", b"\x10\x00" * 800, 100, host_ns=1_100_000_000)
    capture.append_native("mic", b"\x20\x00" * 800, 200, host_ns=1_200_000_000)
    capture.finish()
    (track,) = capture.manifest["tracks"]
    assert track["clockAnchors"] == [[800, 100_000_000], [1600, 200_000_000]]
    assert track["startOffsetMs"] == 0 and track["frames"] == 2400


def test_wire_or_inferred_offsets_are_not_native_clock_observations(tmp_path, monkeypatch):
    capture = session(tmp_path, monkeypatch)
    capture.append_native("mic", b"\x10\x00" * 800, 0)
    capture.append_native("mic", b"\x20\x00" * 800, 100)
    capture.finish()
    assert "clockAnchors" not in capture.manifest["tracks"][0]
