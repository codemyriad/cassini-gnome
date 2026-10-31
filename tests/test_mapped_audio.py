# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - DSP/runtime fixtures follow dependency checks.
"""Analytic clock/gap PCM references shared by speech, turns and voiceprints."""

import hashlib
import wave
from contextlib import closing

import pytest

np = pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Clocked capture PCM uses the shared GStreamer reader", allow_module_level=True)

from library_v2_support import write_wav

from cassini_viewer.diarization import group_tracks
from cassini_viewer.diarization_audio import rejoin
from cassini_viewer.mapped_audio import end_ms, required
from cassini_viewer.speech_pipeline import audio_chunks, audio_segments, resampled_chunks, resampled_stream


def pcm(path):
    with wave.open(str(path)) as source:
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype("<f4") / np.float32(32768)


def collect(track):
    with closing(audio_chunks(track)) as blocks:
        return np.concatenate(list(blocks))


def audio(root, seconds=8, rate=48000, name="track-source.wav"):
    path = root / name
    samples = np.sin(np.arange(rate * seconds) * (2 * np.pi * 211 / rate)) * 0.1
    write_wav(path, samples, rate=rate)
    return path


def track(path, seconds=8, rate=48000, **changes):
    return dict(
        id=path.name,
        path=str(path),
        sessionDirectory=str(path.parent),
        channels=1,
        sampleRate=rate,
        frames=rate * seconds,
        offsetMs=7250,
        kind="microphone",
        speaker="me",
        **changes,
    )


@pytest.mark.parametrize("rate", [16000, 48000])
def test_exact_nominal_clock_preserves_d11_input_bytes_at_both_source_rates(tmp_path, rate):
    path = audio(tmp_path, rate=rate)
    source = track(path, rate=rate, clockAnchors=[[0, 123], [rate * 8, 8_000_000_123]])
    before = path.read_bytes()
    assert not required(source)
    assert np.array_equal(collect(source), np.concatenate(list(resampled_chunks(path))))
    assert path.read_bytes() == before


def test_drift_matches_analytic_linear_sample_map_without_double_offset(tmp_path):
    path = audio(tmp_path)
    source = track(path, clockAnchors=[[0, 0], [48000 * 8, 8_040_000_000]])
    assert required(source)
    actual = collect(source)
    # Independent straight-line reference: 1.005 seconds for each source second.
    original = pcm(path)
    output_count = 385920
    positions = np.arange(output_count, dtype=np.float64) / 1.005
    warped = np.interp(positions, np.arange(len(original)), original, left=0, right=0).astype("<f4")
    expected = np.concatenate(list(resampled_stream(warped[i : i + 48000] for i in range(0, len(warped), 48000))))
    assert len(actual) == 128640 and np.max(np.abs(actual - expected)) < 1e-6
    assert end_ms(source) == 15290  # 7.250 s placement + 8.040 s audio, exactly once.
    assert group_tracks([source])[0]["durationMs"] == 15290


def test_physical_span_gap_is_silence_once_and_fir_context_crosses_boundaries(tmp_path):
    first = audio(tmp_path, seconds=2, name="track-first.wav")
    second = audio(tmp_path, seconds=2, name="track-second.wav")
    source = track(
        first,
        seconds=4,
        spans=[
            dict(file=first.name, frames=96000, sampleRate=48000, channels=1, startOffsetMs=7250),
            dict(file=second.name, frames=96000, sampleRate=48000, channels=1, startOffsetMs=10750),
        ],
    )
    reference = np.concatenate([pcm(first), np.zeros(72000, dtype="<f4"), pcm(second)])
    expected = np.concatenate(list(resampled_stream(reference[i : i + 48000] for i in range(0, len(reference), 48000))))
    actual = collect(source)
    assert len(actual) == 88000 and np.array_equal(actual, expected)
    assert end_ms(source) == 12750
    assert np.max(np.abs(actual[40000:54000])) == 0


def test_speech_segment_pass_turns_and_voiceprints_rejoin_the_same_time_map(tmp_path):
    path = audio(tmp_path)
    source = track(path, clockAnchors=[[0, 0], [48000 * 8, 8_040_000_000]])
    samples = collect(source)
    segments = [dict(startSample=16000, endSample=48000), dict(startSample=64000, endSample=80000)]
    with closing(audio_segments(source, segments)) as chunks:
        actual = list(chunks)
    assert all(
        np.array_equal(block, samples[s["startSample"] : s["endSample"]])
        for block, s in zip(actual, segments, strict=True)
    )
    expected = np.concatenate([np.zeros(7250 * 16, dtype="<f4"), samples])
    with rejoin([source], scratch=tmp_path / "cache") as (stream, frames, sha):
        assert frames == len(expected) and stream.read() == expected.tobytes()
        assert sha == hashlib.sha256(expected.tobytes()).hexdigest()


def test_unsafe_rotated_reference_never_reads_outside_the_session(tmp_path):
    outside = audio(tmp_path, name="track-outside.wav")
    session = tmp_path / "session"
    session.mkdir()
    first = audio(session)
    source = track(
        first, spans=[dict(file=first.name, startOffsetMs=7250), dict(file="../" + outside.name, startOffsetMs=15250)]
    )
    before = outside.read_bytes()
    with pytest.raises(ValueError):
        collect(source)
    assert outside.read_bytes() == before


@pytest.mark.parametrize("rate", [8000, 32000, 44100, 96000, 192000])
def test_other_legal_capture_rates_use_the_shared_48k_path_before_fir(tmp_path, rate):
    path = audio(tmp_path, seconds=2, rate=rate)
    source = track(path, seconds=2, rate=rate)
    assert required(source)
    samples = collect(source)
    assert len(samples) == 32000 and end_ms(source) == 9250
    assert abs(float(np.sqrt(np.mean(samples[500:-500] ** 2))) - 0.1 / np.sqrt(2)) < 0.001
