# SPDX-License-Identifier: GPL-3.0-or-later
"""G11: engine timestamps never extend past the source's own audio."""

import hashlib

import pytest

np = pytest.importorskip("numpy")
from cassini_talk.speech_pipeline import recognise_segment  # noqa: E402


class OverrunModel:
    def transcribe(self, samples, *, language, word_timestamps):
        assert language is None and word_timestamps is True
        return dict(
            text="before word after",
            language="en",
            words=[
                dict(word="before", start=-0.1, end=0.1),
                dict(word="word", start=0.2, end=5),
                dict(word="after", start=6, end=7),
            ],
        )


@pytest.mark.parametrize(
    "count,expected_end",
    [(12000, 9000), (16000, 9250), (20000, 9250)],
    ids=["short-decoder-output", "exact-segment", "segment-bound"],
)
def test_engine_extent_is_bounded_by_segment_and_actual_samples(count, expected_end):
    samples = np.ones(count, dtype="<f4")
    segment = dict(startSample=16000, endSample=32000, reason="silence")
    value, words, language, sha = recognise_segment(OverrunModel(), None, segment, 7250.9, samples=samples)
    assert value == dict(segment, text="before word after") and language == "en"
    assert sha == hashlib.sha256(samples.tobytes()).hexdigest()
    assert words == [
        dict(text="before", startMs=8250, endMs=8350),
        dict(text="word", startMs=8450, endMs=expected_end),
        dict(text="after", startMs=expected_end, endMs=expected_end),
    ]


def test_mapped_source_end_is_the_final_bound_even_with_a_longer_decoded_segment():
    samples = np.ones(16000, dtype="<f4")
    _, words, _, _ = recognise_segment(
        OverrunModel(),
        None,
        dict(startSample=16000, endSample=32000),
        7250,
        samples=samples,
        audio_end_ms=8801,
    )
    assert words[1] == dict(text="word", startMs=8450, endMs=8801)
    assert words[2] == dict(text="after", startMs=8801, endMs=8801)


def test_clamp_uses_absolute_sample_endpoint_without_accumulating_rounding():
    _, words, _, _ = recognise_segment(
        OverrunModel(), None, dict(startSample=15, endSample=30), 7250, samples=np.ones(15, dtype="<f4")
    )
    assert all(7250 <= word["startMs"] <= word["endMs"] <= 7251 for word in words)
    assert words[-1]["endMs"] == 7251


def test_process_tracks_uses_the_clock_mapped_source_end(tmp_path, monkeypatch):
    import wave

    from cassini_talk import speech_pipeline as pipeline

    path = tmp_path / "track-clocked.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", ""))
        stream.writeframes(np.full(16000, 1234, dtype="<i2").tobytes())
    before = path.read_bytes()

    class Detector:
        frame_size = 512
        runtime = {}

        def __init__(self, *_):
            pass

        def probability(self, _):
            return 1.0

        def reset(self):
            pass

    monkeypatch.setattr(pipeline, "SpeechDetector", Detector)
    events = []
    pipeline.process_tracks(
        OverrunModel(),
        [
            dict(
                id="clocked",
                path=str(path),
                offsetMs=7250,
                sampleRate=16000,
                frames=16000,
                channels=1,
                clockAnchors=[[0, 0], [16000, 1_005_000_000]],
            )
        ],
        None,
        lambda kind, **values: events.append(dict(type=kind, **values)),
    )
    progress = [event for event in events if event["type"] == "progress"]
    assert progress and max(word["endMs"] for event in progress for word in event["words"]) == 8255
    assert all(7250 <= word["startMs"] <= word["endMs"] <= 8255 for event in progress for word in event["words"])
    assert path.read_bytes() == before
