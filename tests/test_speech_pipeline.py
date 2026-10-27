# SPDX-License-Identifier: GPL-3.0-or-later
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
from cassini_viewer.speech_pipeline import SpeechGate, resampled_chunks  # noqa: E402

ROOT = Path(__file__).resolve().parent / "whistle/pipeline"


def test_shared_fir_golden_and_random_access_edges_are_exact():
    assert (ROOT / "fir-48k-16k.json").read_bytes() == (
        ROOT.parents[2] / "src/cassini_viewer/fir-48k-16k.json"
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
    from cassini_viewer import speech_pipeline as pipeline

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

    from cassini_viewer.speech_pipeline import TAPS, resampled_stream

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
    from cassini_viewer import speech_pipeline as pipeline

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
