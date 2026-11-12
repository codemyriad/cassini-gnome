# SPDX-License-Identifier: GPL-3.0-or-later
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
from cassini_talk.speech_pipeline import SpeechGate, resampled_chunks  # noqa: E402

ROOT = Path(__file__).resolve().parent / "whistle/pipeline"


@pytest.mark.parametrize("channels", [1, 2], ids=["mono", "stereo"])
def test_all_pcm16_values_and_asymmetric_stereo_normalize_exactly(tmp_path, channels):
    values = np.arange(-32768, 32768, dtype="<i2")
    pcm = values if channels == 1 else np.column_stack((values, np.roll(values, 7919)))
    # Integer arithmetic defines the downmix independently of float reduction.
    expected = pcm.astype(np.int64)
    if channels == 2:
        expected = expected.sum(axis=1)
    expected = (expected / (32768 * channels)).astype("<f4")
    path = tmp_path / "pcm.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, 16000, 0, "NONE", ""))
        stream.writeframes(pcm.tobytes())
    actual = np.concatenate(list(resampled_chunks(path, chunk_size=1009)))
    assert actual.tobytes() == expected.tobytes()
    assert path.read_bytes()[44:] == pcm.tobytes()


def test_shared_fir_golden_and_random_access_edges_are_exact():
    assert (ROOT / "fir-48k-16k.json").read_bytes() == (
        ROOT.parents[2] / "src/cassini_talk/fir-48k-16k.json"
    ).read_bytes()
    path = ROOT / "pipeline-input-48k.wav"
    expected = np.fromfile(ROOT / "pipeline-resampled-16k.f32", dtype="<f4")
    actual = np.concatenate(list(resampled_chunks(path)))
    assert np.max(np.abs(actual - expected)) <= 1e-6
    for start, end in [(0, 17), (15997, 16017), (len(actual) - 20, len(actual))]:
        value = np.concatenate(list(resampled_chunks(path, start, end, chunk_size=7)))
        np.testing.assert_array_equal(value, actual[start:end])


def test_stereo_is_averaged_before_fir_and_other_rates_are_rejected(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((2, 2, 48000, 0, "NONE", ""))
        stream.writeframes(np.tile(np.array([1000, -1000], dtype="<i2"), 480).tobytes())
    assert not np.any(np.concatenate(list(resampled_chunks(path))))
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", ""))
        stream.writeframes(bytes(800))
    with pytest.raises(ValueError, match="16 or 48"):
        list(resampled_chunks(path))


def test_gate_vote_hysteresis_preroll_and_silence_tail():
    class Detector:
        frame_size = 512

        def probability(self, frame):
            return float(frame[0])

        def reset(self):
            pass

    gate = SpeechGate(Detector())
    for probability in (0.51, 0.1, 0.51, 0.4):
        gate.feed(np.full(512, probability, dtype="<f4"))
    assert gate.active and gate.quiet == 0
    gate = SpeechGate(Detector())
    chunks = []
    for probability in [0] * 20 + [1] * 10 + [0] * 19:
        chunks += gate.feed(np.full(512, probability, dtype="<f4"))
    assert chunks[0].start_sample == 6144
    assert chunks[-1].start_sample + len(chunks[-1].samples) == 25088
    assert chunks[-1].end and chunks[-1].reason == "silence"


def test_gate_caps_long_speech_with_live_exact_frame_boundary():
    class Detector:
        frame_size = 512

        def probability(self, frame):
            return 1.0

        def reset(self):
            pass

    gate = SpeechGate(Detector())
    chunks = gate.feed(np.ones(16 * 16000, dtype="<f4")) + gate.flush()
    ends = [chunk for chunk in chunks if chunk.end]
    assert ends[0].start_sample + len(ends[0].samples) == 240128
    assert ends[0].reason == "durationLimit"
    assert ends[-1].start_sample + len(ends[-1].samples) == 256000


def test_automatic_language_and_unknown_offset_zero_with_diagnostic(tmp_path, monkeypatch):
    from cassini_talk import speech_pipeline as pipeline

    path = tmp_path / "mono.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", ""))
        stream.writeframes(bytes(3200))

    class Detector:
        frame_size = 512
        runtime = {}

        def __init__(self, *args):
            pass

        def probability(self, frame):
            return 1.0

        def reset(self):
            pass

    class Model:
        def transcribe(self, samples, language, word_timestamps):
            assert language is None and word_timestamps is True
            return dict(text="word", language="en", words=[dict(word="word", start=0.033, end=0.1)])

    monkeypatch.setattr(pipeline, "SpeechDetector", Detector)
    events = []
    result = pipeline.process_tracks(
        Model(),
        [dict(id="one", path=str(path), offsetMs=None)],
        None,
        lambda kind, **values: events.append(dict(type=kind, **values)),
    )
    assert result["diagnostics"] == [dict(code="unknown-offset-display-zero", track="one")]
    assert events[-1]["words"] == [dict(startMs=33, endMs=100, text="word")]


def test_streamed_fir_matches_immutable_reference_and_arbitrary_input_boundaries():
    import wave
    from pathlib import Path

    from cassini_talk.speech_pipeline import TAPS, resampled_stream

    root = Path(__file__).parent / "whistle/pipeline"
    with wave.open(str(root / "pipeline-input-48k.wav")) as audio:
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / np.float32(32768)

    # Deliberately split before/after the centred FIR context and at nonmultiples of three.
    def chunks(values):
        sizes = (1, 2, 47, 49, 997, 48000, 31)
        offset = index = 0
        while offset < len(values):
            length = sizes[index % len(sizes)]
            yield values[offset : offset + length]
            offset += length
            index += 1

    result = np.concatenate(list(resampled_stream(chunks(pcm))))
    expected = np.fromfile(root / "pipeline-resampled-16k.f32", dtype="<f4")
    assert np.array_equal(result, expected)
    for length in (1, 2, 47, 48, 49, 48000, 48017):
        small = pcm[:length]
        result = np.concatenate(list(resampled_stream(chunks(small))))
        reference = np.array(
            [
                sum(float(h) * float(small[3 * k + 48 - n]) for n, h in enumerate(TAPS) if 0 <= 3 * k + 48 - n < length)
                for k in range((length + 2) // 3)
            ],
            dtype="<f4",
        )
        assert np.array_equal(result, reference), length


def test_received_second_pass_reports_progress_while_skipping_a_long_quiet_span(monkeypatch):
    from cassini_talk import speech_pipeline as pipeline

    def chunks(_track):
        for _ in range(10):
            yield np.zeros(16000, dtype="<f4")

    ticks = iter(range(100))
    monkeypatch.setattr(pipeline, "audio_chunks", chunks)
    notices = []
    segments = [dict(startSample=9 * 16000, endSample=10 * 16000)]
    result = list(
        pipeline.audio_segments(dict(format="opus"), segments, lambda: notices.append(True), clock=lambda: next(ticks))
    )
    assert len(notices) >= 9 and len(result[0]) == 16000


@pytest.mark.parametrize(
    "length,low,first,last",
    [
        (0, 0, 0, 1),
        (1, 0, 0, 1),
        (2, 0, 0, 1),
        (47, 0, 0, 16),
        (48, 0, 0, 16),
        (49, 0, 0, 17),
        (95, 0, 0, 32),
        (96, 0, 0, 32),
        (97, 0, 0, 33),
        (211, 7, 0, 80),
        (211, 49, 17, 81),
        (211, 97, 50, 112),
        (211, 0, 7, 7),
    ],
)
def test_fir_slice_boundaries_keep_scalar_pcm_bytes(length, low, first, last):
    from cassini_talk.speech_pipeline import TAPS, fir_values

    samples = np.random.default_rng(81).uniform(-1, 1, length).astype("<f4")
    expected = []
    # Independent scalar FIR definition, including zero padding. Every tap is
    # added in order with a separate double-precision multiply and add.
    for position in range(first, last):
        total = 0.0
        for tap, coefficient in enumerate(TAPS):
            index = position * 3 + 48 - tap - low
            if 0 <= index < len(samples):
                total += float(coefficient) * float(samples[index])
        expected.append(total)
    assert fir_values(samples, low, first, last).tobytes() == np.asarray(expected, dtype="<f4").tobytes()


def test_recognition_cache_skips_cached_segments_and_invalidates_on_model_mismatch(tmp_path, monkeypatch):
    import json
    import wave

    from cassini_talk import speech_pipeline as pipeline

    path = tmp_path / "two-segments.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        # Segment 1 (0..0.5s speech), 1.0s silence (0.5..1.5s), Segment 2 (1.5..2.0s speech).
        speech_a = np.full(8000, 4000, dtype="<i2")
        silence = np.zeros(16000, dtype="<i2")
        speech_b = np.full(8000, 6000, dtype="<i2")
        wav.writeframes(np.concatenate((speech_a, silence, speech_b)).tobytes())

    class Detector:
        frame_size = 512
        runtime = dict(inputStage="fake")

        def __init__(self, *_args):
            pass

        def probability(self, frame):
            return 1.0 if float(np.abs(frame).max()) > 0.01 else 0.0

        def reset(self):
            pass

    monkeypatch.setattr(pipeline, "SpeechDetector", Detector)
    transcribe_calls = []

    class Model:
        def transcribe(self, samples, language, word_timestamps):
            assert language is None and word_timestamps is True
            idx = len(transcribe_calls)
            transcribe_calls.append(float(np.abs(samples).mean()))
            label = "first" if idx % 2 == 0 else "second"
            return dict(text=label, language="en", words=[dict(word=label, start=0.05, end=0.25)])

    initial_events = []
    pipeline.process_tracks(
        Model(),
        [dict(id="mic", path=str(path), offsetMs=0)],
        None,
        lambda kind, **values: initial_events.append(dict(type=kind, **values)),
    )
    progress_events = [e for e in initial_events if e["type"] == "progress"]
    assert len(progress_events) == 2 and len(transcribe_calls) == 2
    first_entry = progress_events[0]["cacheEntry"]

    cache_file = tmp_path / ".recognition-cache.json"
    cache_file.write_text(
        json.dumps(
            dict(
                format=pipeline.RECOGNITION_CACHE_FORMAT,
                model="whistle",
                segments={
                    first_entry["key"]: {
                        "text": first_entry["text"],
                        "words": first_entry["words"],
                        "language": first_entry["language"],
                    }
                },
            )
        )
    )

    resumed_calls = []

    class ResumedModel:
        def transcribe(self, samples, language, word_timestamps):
            resumed_calls.append(len(samples))
            return dict(text="second", language="en", words=[dict(word="second", start=0.05, end=0.25)])

    resumed_events = []
    pipeline.process_tracks(
        ResumedModel(),
        [dict(id="mic", path=str(path), offsetMs=0)],
        None,
        lambda kind, **values: resumed_events.append(dict(type=kind, **values)),
        recognition_cache=dict(path=str(cache_file), model="whistle"),
    )
    resumed_progress = [e for e in resumed_events if e["type"] == "progress"]
    assert len(resumed_calls) == 1, "Cached first segment must skip neural inference"
    assert [e["words"] for e in resumed_progress] == [e["words"] for e in progress_events]
    assert "cacheEntry" not in resumed_progress[0] and "cacheEntry" in resumed_progress[1]

    mismatch_calls = []

    class MismatchModel:
        def transcribe(self, samples, language, word_timestamps):
            mismatch_calls.append(len(samples))
            return dict(text="recomputed", language="en", words=[dict(word="recomputed", start=0.05, end=0.25)])

    pipeline.process_tracks(
        MismatchModel(),
        [dict(id="mic", path=str(path), offsetMs=0)],
        None,
        lambda *_args, **_kw: None,
        recognition_cache=dict(path=str(cache_file), model="parakeet@different"),
    )
    assert len(mismatch_calls) == 2, "Model mismatch must ignore stale recognition cache"
