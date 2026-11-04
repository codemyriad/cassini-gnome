# SPDX-License-Identifier: GPL-3.0-or-later
"""Measured native host stamps, sparse over hours; no device/arrival-time guesses."""

import json
import wave
from types import SimpleNamespace

import pytest

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


def test_anchored_end_starts_at_the_d55_placement_of_a_fractional_offset():
    # 31999.96875 ms is 1535998.5 frames at 48 kHz: D55 places the first sample at 1535999,
    # where the publication's Mapping puts it; 10.000015 s elapsed is 480000.72 frames.
    pytest.importorskip("numpy")

    from cassini_talk.capture_clock import mapped_end_sample
    from cassini_talk.library_v2.mixdown import Mapping

    offset, frames, anchors = 31999.96875, 480000, ((0, 5_000_000_000), (480000, 15_000_015_000))
    span = SimpleNamespace(info=SimpleNamespace(sample_rate=48000), start_ms=offset, anchors=anchors)
    assert Mapping(span).end(frames) == 1535999 + 480001
    # Rotation writes this end / 48 as the next span's startOffsetMs, so it stays on Mapping's end.
    assert mapped_end_sample(frames, 48000, offset, anchors) == Mapping(span).end(frames)


def test_unanchored_end_starts_at_the_d55_placement_of_a_half_frame_offset():
    # Without two anchors the span is nominal: 1535998.5 frames is placed at 1535999 (half away
    # from zero, D55), never at 1535998 (round half to even), where Mapping places it too.
    pytest.importorskip("numpy")

    from cassini_talk.capture_clock import mapped_end_sample
    from cassini_talk.library_v2.mixdown import Mapping

    span = SimpleNamespace(info=SimpleNamespace(sample_rate=48000), start_ms=31999.96875, anchors=())
    assert mapped_end_sample(480000, 48000, 31999.96875, ()) == Mapping(span).end(480000) == 1535999 + 480000
    assert mapped_end_sample(160000, 16000, 31999.96875, ((0, 5_000_000_000),)) == 1535999 + 480000
