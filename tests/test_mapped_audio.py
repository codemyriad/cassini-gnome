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

from cassini_talk.diarization import group_tracks
from cassini_talk.diarization_audio import rejoin
from cassini_talk.mapped_audio import end_ms, required
from cassini_talk.speech_pipeline import audio_chunks, audio_segments, resampled_chunks, resampled_stream


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


def test_raw_playback_and_export_use_the_same_drift_map_as_publication(tmp_path):
    from cassini_talk.export_audio import track_pcm
    from cassini_talk.library import CaptureTrack

    path = audio(tmp_path)
    source = CaptureTrack(path, 8 * 48000, 48000, 7250, True, clock_anchors=((0, 0), (8 * 48000, 8_040_000_000)))
    actual = np.concatenate([np.asarray(block) for block in track_pcm(source)])
    assert len(actual) == 733920
    positions = (np.arange(len(actual), dtype=np.float64) - 348000) / 1.005
    original = pcm(path)
    expected = np.interp(positions, np.arange(len(original)), original, left=0, right=0)
    assert np.max(np.abs(actual - expected)) < 1e-6
    assert source.display_end_ms == 15290


@pytest.mark.parametrize("drift", [-100, 100])
def test_ten_hour_clocked_source_seeks_directly_with_bounded_reads(tmp_path, monkeypatch, drift):
    import os

    from library_v2_support import wav_header

    from cassini_talk.folder_player import PCMSource
    from cassini_talk.library import CaptureTrack

    path = tmp_path / "track-ten-hours.wav"
    frames = 10 * 3600 * 48000
    with path.open("wb") as stream:
        stream.write(wav_header(frames, 48000, 1))
        stream.truncate(44 + frames * 2)
        stream.seek(44 + (frames - 48000) * 2)
        stream.write(np.full(48000, 5000, dtype="<i2").tobytes())
    elapsed_ns = 10 * 3600 * 1_000_000_000 * (1_000_000 + drift) // 1_000_000
    track = CaptureTrack(path, frames, 48000, 6000, True, clock_anchors=((0, 0), (frames, elapsed_ns)))
    source = PCMSource(track)
    reads = []
    original = os.pread

    def observed(fd, count, offset):
        reads.append((count, offset))
        return original(fd, count, offset)

    monkeypatch.setattr(os, "pread", observed)
    try:
        source._open()
        tail = source.end_frame - 10000
        assert source._seek(None, tail * 1_000_000_000 // 48000)
        values = np.frombuffer(source._mapped_data(source.cursor, 2048), dtype="<f4")
        assert np.all(values == np.float32(5000 / 32768))
        assert sum(size for size, _ in reads) < 10000, "A late seek decoded the ten-hour prefix"
        assert len(reads) == 1 and reads[0][1] > path.stat().st_size - 100000
        assert source._seek(None, 0)
        assert not np.any(np.frombuffer(source._mapped_data(0, 2048), dtype="<f4"))
        assert len(reads) == 1, "Leading silence opened or decoded more source PCM"
        assert track.display_end_ms == 6000 + elapsed_ns // 1_000_000
    finally:
        source.close()


@pytest.mark.parametrize("rate", [8000, 16000, 32000, 44100, 48000, 96000, 192000])
@pytest.mark.parametrize("offset", [0, 7250.5, 3600000])
def test_scalar_duration_is_the_core_mapping_endpoint_without_numpy_on_gtk(rate, offset):
    from types import SimpleNamespace

    from cassini_talk.capture_clock import mapped_end_sample
    from cassini_talk.library_v2.mixdown import Mapping

    frames = 10 * 3600 * rate
    anchors = ((0, 19_123_456_000), (frames // 3, 12_020_323_456_000), (frames, 36_025_523_456_000))
    span = SimpleNamespace(info=SimpleNamespace(sample_rate=rate), start_ms=offset, anchors=anchors)
    assert mapped_end_sample(frames, rate, offset, anchors) == Mapping(span).end(round(frames * 48000 / rate))


class OneWord:
    """A recognizer that hears one word at the very start of its segment."""

    def transcribe(self, _samples, **_options):
        return dict(text="step", language="en", words=[dict(word="step", start=0.0, end=0.05)])


def rises(samples, level=0.125):
    """Samples where a 0.25 step first reaches half its height: the FIR's centre tap is on its first sample."""
    above = np.asarray(samples) >= level
    return [int(i) for i in np.flatnonzero(above & ~np.concatenate(([False], above[:-1])))]


def rotated(root, rate, anchors):
    """Codex #209: two physical spans whose D55 placements are not a whole 48 kHz frame apart."""
    first, second = root / "track-first.wav", root / "track-second.wav"
    count = rate // 4
    write_wav(first, np.full(count, 0.25), rate=rate)
    write_wav(second, np.full(count, 0.25), rate=rate)
    spans = [
        dict(file=first.name, frames=count, sampleRate=rate, channels=1, startOffsetMs=1001.28125),
        dict(file=second.name, frames=count, sampleRate=rate, channels=1, startOffsetMs=1999.9375),
    ]
    if anchors:
        # The second file's own clock runs 0.05 % slow; placement stays its startOffsetMs.
        spans[1]["clockAnchors"] = [[0, 0], [count, count * 1_000_500_000 // rate]]
    return dict(track(first, rate=rate), offsetMs=1001.28125, frames=2 * count, spans=spans)


@pytest.mark.parametrize(
    "rate,anchors", [(48000, False), (16000, False), (48000, True)], ids=["48k", "16k", "48k-clock-anchored"]
)
def test_rotated_spans_start_at_their_own_d55_placement_in_the_rejoin_and_word_times(tmp_path, rate, anchors):
    from cassini_talk.placement import frame
    from cassini_talk.speech_pipeline import placed_offset, recognise_segment

    source = rotated(tmp_path, rate, anchors)
    assert required(source)
    # D55: each file's first sample is session sample frame(startOffsetMs, 16000), here
    # 16021 and 31999. The raw 998.65625 ms between them is 47935.5 frames at 48 kHz:
    # shifting by it rounds once there and again when the track's placement is added.
    placed = [frame(1001.28125, 16000), frame(1999.9375, 16000)]
    assert placed == [16021, 31999]
    with rejoin([source], scratch=tmp_path / "cache") as (stream, _frames, _sha):
        session = np.frombuffer(stream.read(), dtype="<f4")
    local = collect(source)
    offset = placed_offset(source, [])
    words = []
    for start in rises(local):
        segment = dict(startSample=start, endSample=start + 1600, reason="silence")
        _value, (word,), _language, _sha = recognise_segment(
            OneWord(), None, segment, offset, samples=local[start : start + 1600]
        )
        words.append(word["startMs"])
    # Session ms floor the session sample: 16021 -> 1001 ms, 31999 -> 1999 ms (never 2000).
    # One assertion, so a miss shows the rejoin, the recognizer input and the words together.
    assert (rises(session), rises(local), words) == (placed, [0, 31999 - 16021], [1001, 1999])
