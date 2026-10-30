# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent frozen D36 oracle through actual libgstfft and streaming PCM."""

import array
import ctypes.util
import hashlib
import json
import math
import wave
from pathlib import Path

import pytest

from cassini_viewer.spectro_analysis import BANDS, EDGES, FFT, Quantizer, Track

ROOT = Path(__file__).parent / "spectro-vectors"


@pytest.fixture(autouse=True)
def real_fft():
    if ctypes.util.find_library("gstfft-1.0") is None:
        pytest.skip("Install GStreamer base for the required recording/GUI gate")


def test_shared_vectors_have_unchanged_published_hashes():
    for name, digest in json.loads((ROOT / "UPSTREAM.json").read_text())["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("chunk", [480, 8192])
def test_streaming_real_fft_matches_every_frozen_raw_and_display_cell(chunk):
    golden = json.loads((ROOT / "spectro-v1.golden.json").read_text())
    assert golden["spec"] == "cassini-spectro-v1" and golden["firstFrame"] == 2
    assert golden["edgesHz"] == [round(edge, 3) for edge in EDGES]
    track, raw, display = Track(), Quantizer(release=math.inf), Quantizer()
    actual_raw, actual_display, slots = [], [], []
    with wave.open(str(ROOT / "spectro-v1.wav")) as stream:
        while data := stream.readframes(chunk):
            for slot, powers in track.feed(data):
                slots.append(slot)
                actual_raw.append(list(raw.column(slot, powers)))
                actual_display.append(list(display.column(slot, powers)))
    assert slots == list(range(1, 399))
    for kind, actual in (("raw", actual_raw), ("display", actual_display)):
        assert len(actual) == len(golden[kind]) == 398
        delta = [
            abs(a - b)
            for row, expected in zip(actual, golden[kind], strict=True)
            for a, b in zip(row, expected, strict=True)
        ]
        assert max(delta) <= 3
        assert sum(value <= 1 for value in delta) / len(delta) >= 0.99
    assert all(not any(row) for row in actual_raw[:48])
    assert all(not any(row) for row in actual_raw[200:248])
    assert len(track.history) == track.fft.size


def tone(frames, rate=48000, channels=1, *, frequency=1000, amplitude=32767):
    mono = [round(amplitude * math.sin(2 * math.pi * frequency * i / rate)) for i in range(frames)]
    return array.array("h", (sample for value in mono for sample in [value] * channels)).tobytes()


def test_sine_normalisation_and_power_sum_before_quantisation():
    engine = FFT(48000)
    frame = array.array("f", (32767 * math.sin(2 * math.pi * 1000 * i / 48000) for i in range(engine.size)))
    powers = engine.powers(frame)
    assert 10 * math.log10(sum(powers)) == pytest.approx(-3.0106, abs=0.001)
    mixed = [a + b for a, b in zip(powers, powers, strict=True)]
    assert 10 * math.log10(sum(mixed)) - 10 * math.log10(sum(powers)) == pytest.approx(3.0103, abs=0.0001)
    assert len(powers) == BANDS


def test_stereo_is_mean_of_channels_and_offsets_stamp_frame_centres():
    mono, stereo = Track(start_ms=1000.25), Track(channels=2, start_ms=1000.25)
    a, b = mono.feed(tone(9600)), stereo.feed(tone(9600, channels=2))
    assert a == b and a[0][0] == 51
    # Opposite channels cancel; never analyse one side only.
    pcm = array.array("h", (sample for value in range(4800) for sample in (value, -value))).tobytes()
    rows = Track(channels=2).feed(pcm)
    assert rows and all(not any(Quantizer().column(slot, powers)) for slot, powers in rows)


def test_no_frame_crosses_a_gap_and_release_restarts_after_it():
    track = Track()
    before = track.feed(tone(9600))
    assert before[-1][0] == 8
    track.skip(48000)
    assert not track.feed(bytes(2047 * 2))
    after = track.feed(bytes(4096 * 2))
    assert after and after[0][0] >= 61
    q = Quantizer()
    assert max(q.column(before[-1][0], before[-1][1])) > 200
    assert not any(q.column(after[0][0], after[0][1]))
    with pytest.raises(ValueError, match="Partial"):
        track.feed(b"x")


def test_unselected_tracks_keep_bounded_warm_history_without_fft(monkeypatch):
    track = Track()
    powers, called = track.fft.powers, []
    monkeypatch.setattr(track.fft, "powers", lambda frame: (called.append(True), powers(frame))[1])
    for _ in range(10):
        assert not track.feed(tone(8192), selected=False)
        assert len(track.history) == track.fft.size
    assert not called
    assert track.feed(tone(960)) and called


def test_other_rates_follow_the_published_frame_table_and_stationary_power():
    levels = []
    for rate, expected in ((16000, 1024), (24000, 1024), (44100, 2048), (48000, 2048), (96000, 4096)):
        track = Track(rate)
        assert track.fft.size == expected
        frames = track.feed(tone(rate // 2, rate=rate, amplitude=5000))
        levels.append(10 * math.log10(sum(frames[-1][1])))
    assert max(levels) - min(levels) < 0.01
    track = Track(8000)
    assert all(not value for value in track.feed(tone(8000, rate=8000))[-1][1][-8:])
