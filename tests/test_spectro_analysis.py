# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent frozen D36 oracle through actual libgstfft and streaming PCM."""

import array
import ctypes.util
import hashlib
import json
import math
import sys
import wave
from pathlib import Path

import pytest

from cassini_talk.spectro_analysis import BANDS, EDGES, FFT, Quantizer, Track

ROOT = Path(__file__).parent / "spectro-vectors"


@pytest.fixture(autouse=True)
def real_fft():
    if ctypes.util.find_library("gstfft-1.0") is None:
        pytest.skip("Install GStreamer base for the required recording/GUI gate")


def test_vectors_match_their_pinned_inputs_and_reference_generated_hashes():
    pins = json.loads((ROOT / "UPSTREAM.json").read_text())
    for name, digest in dict(pins["files"], **pins["inputs"]).items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("accelerated", [False, True])
@pytest.mark.parametrize("chunk", [480, 8192])
@pytest.mark.parametrize("name", ["spectro-v1", "dc-quiet-jfk-v1"])
def test_streaming_real_fft_matches_every_frozen_raw_and_display_cell(chunk, name, accelerated, monkeypatch):
    from cassini_talk import spectro_analysis

    helper = pytest.importorskip("numpy") if accelerated else None
    monkeypatch.setattr(spectro_analysis, "acceleration", lambda: helper)
    golden = json.loads((ROOT / (name + ".golden.json")).read_text())
    assert golden["spec"] == "cassini-spectro-v1" and golden["firstFrame"] == 2
    assert golden["edgesHz"] == [round(edge, 3) for edge in EDGES]
    track, raw, display = Track(), Quantizer(release=math.inf), Quantizer()
    actual_raw, actual_display, slots = [], [], []
    path = ROOT / (name + ".wav") if name == "spectro-v1" else ROOT.parent / "dc" / (name + ".wav")
    with wave.open(str(path)) as stream:
        while data := stream.readframes(chunk):
            for slot, powers in track.feed(data):
                slots.append(slot)
                actual_raw.append(list(raw.column(slot, powers)))
                actual_display.append(list(display.column(slot, powers)))
    assert slots == list(range(1, len(golden["raw"]) + 1))
    for kind, actual in (("raw", actual_raw), ("display", actual_display)):
        assert len(actual) == len(golden[kind])
        delta = [
            abs(a - b)
            for row, expected in zip(actual, golden[kind], strict=True)
            for a, b in zip(row, expected, strict=True)
        ]
        assert max(delta) <= 3
        assert sum(value <= 1 for value in delta) / len(delta) >= 0.99
    if name == "spectro-v1":
        assert all(not any(row) for row in actual_raw[:48])
        assert all(not any(row) for row in actual_raw[200:248])
    # Any whole constant frame (zero or DC offset) must be exactly empty.
    with wave.open(str(path)) as stream:
        samples = array.array("h", stream.readframes(stream.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    constant = 0
    for index, row in enumerate(actual_raw):
        end = (index + 3) * 960
        frame = samples[end - 2048 : end]
        if min(frame) == max(frame):
            constant += 1
            assert not any(row)
    assert constant > 0
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


@pytest.mark.parametrize("channels", [1, 2])
def test_unselected_tracks_copy_without_conversion_or_fft_and_resume_warm(monkeypatch, channels):
    track, control = Track(channels=channels), Track(channels=channels)
    mono = track._mono
    monkeypatch.setattr(track, "_mono", lambda *_: pytest.fail("Unselected PCM was converted"))
    pcm = tone(8192, channels=channels)
    for _ in range(10):
        assert not track.feed(pcm, selected=False)
        control.feed(pcm)
        assert len(track.raw_history) == max(track.size, track.rate // 10) * channels * 2
        assert not track.history and track._fft is None
    monkeypatch.setattr(track, "_mono", mono)
    resumed = tone(960, channels=channels)
    assert track.feed(resumed) == control.feed(resumed)
    assert track.feed(tone(960, channels=channels), selected=False) == []
    assert track._fft is None


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


@pytest.mark.parametrize("accelerated", [False, True])
def test_stereo_mean_keeps_half_sample_precision_with_both_helpers(monkeypatch, accelerated):
    from cassini_talk import spectro_analysis

    helper = pytest.importorskip("numpy") if accelerated else None
    monkeypatch.setattr(spectro_analysis, "acceleration", lambda: helper)
    stereo = array.array("h", [100, -99, -100, 99, 32767, -32768]).tobytes()
    assert list(Track(channels=2)._mono(stereo)) == [0.5, -0.5, -0.5]


def test_numeric_helper_preparation_returns_before_loading_and_fails_to_pure_arrays(monkeypatch):
    import builtins
    from types import SimpleNamespace

    from cassini_talk import spectro_analysis

    tasks = []

    class Worker:
        def __init__(self, *, target, name, daemon):
            assert name == "cassini-spectro-prepare" and daemon
            self.target = target

        def start(self):
            tasks.append(self.target)

    monkeypatch.setattr(spectro_analysis, "_PREPARATION", None)
    monkeypatch.setattr(spectro_analysis, "threading", SimpleNamespace(Thread=Worker))
    future = spectro_analysis.prepare_acceleration()
    assert not future.done() and spectro_analysis.acceleration() is None
    assert spectro_analysis.prepare_acceleration() is future and len(tasks) == 1
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("optional helper unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    tasks[0]()
    assert future.done() and spectro_analysis.acceleration() is None
    assert Track()._mono(b"\x01\x00").tolist() == [1.0]
