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


class Detector:
    frame_size = 512
    runtime = {}

    def __init__(self, *_):
        pass

    def probability(self, _):
        return 1.0

    def reset(self):
        pass


def spy_audio_end(monkeypatch):
    from cassini_talk import speech_pipeline as pipeline

    supplied = []
    original = pipeline.recognise_segment

    def recognise(*args, audio_end_ms=None, **kwargs):
        supplied.append(audio_end_ms)
        return original(*args, audio_end_ms=audio_end_ms, **kwargs)

    monkeypatch.setattr(pipeline, "recognise_segment", recognise)
    return supplied


def write_wav(path, frames):
    import wave

    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", ""))
        stream.writeframes(np.full(frames, 1234, dtype="<i2").tobytes())


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


def test_clamp_counts_the_segment_pcm_in_whole_ms_from_its_first_sample_as_mac_does():
    # Session samples 116015..116030 are 7250.9375..7251.875 ms: the segment starts at 7250
    # ms and its 15 samples are 0 whole ms, so nothing ends past 7250 (Mac: start + 15 * 1000 / 16000).
    _, words, _, _ = recognise_segment(
        OverrunModel(), None, dict(startSample=15, endSample=30), 7250, samples=np.ones(15, dtype="<f4")
    )
    assert all(word["startMs"] == word["endMs"] == 7250 for word in words)


def mac_clamped(offset_ms, start_sample, count, start, end):
    """MeetingTranscriber.clamped over words(), as Swift computes it (Int64 division truncates)."""
    from fractions import Fraction

    placed = Fraction(offset_ms) * 16  # TrackPlacement.frame: half away from zero, exact here
    first = int(placed) + (placed - int(placed) >= Fraction(1, 2))
    base, length = (first + start_sample) * 1000 // 16000, count * 1000 // 16000
    a, b = round(start * 1000), round(end * 1000)  # .toNearestOrEven, as Python's round
    lower = min(a, length)
    return base + lower, base + min(max(b, lower), length)


def test_word_past_the_pcm_ends_where_mac_ends_it():
    class Late:  # a recognizer that reports a word past the PCM it was given (G11)
        def transcribe(self, samples, **_options):
            return dict(text="x", language="en", words=[dict(word="x", start=0.2, end=9.0)])

    differences = []
    for offset in (1234.5104, 31999.96875, 1000.09375, 0, 17.3, 250.0625):
        for start in (0, 512, 1000, 16008, 31744):
            for count in range(3990, 4030):
                segment = dict(startSample=start, endSample=start + count, reason="silence")
                _, (word,), _, _ = recognise_segment(Late(), None, segment, offset, samples=np.zeros(count, "<f4"))
                if (word["startMs"], word["endMs"]) != mac_clamped(offset, start, count, 0.2, 9.0):
                    differences.append((offset, start, count, word))
    assert differences == []


def test_process_tracks_uses_the_clock_mapped_source_end(tmp_path, monkeypatch):
    from cassini_talk import speech_pipeline as pipeline

    path = tmp_path / "track-clocked.wav"
    write_wav(path, 16000)
    before = path.read_bytes()

    monkeypatch.setattr(pipeline, "SpeechDetector", Detector)
    supplied = spy_audio_end(monkeypatch)
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
    # The mapped end is what bounds the words, not only the segment extent.
    assert supplied and set(supplied) == {8255}


def run_nominal(monkeypatch, track):
    from cassini_talk import speech_pipeline as pipeline

    monkeypatch.setattr(pipeline, "SpeechDetector", Detector)
    supplied = spy_audio_end(monkeypatch)
    events = []
    pipeline.process_tracks(
        OverrunModel(), [track], None, lambda kind, **values: events.append(dict(type=kind, **values))
    )
    words = [word for event in events if event["type"] == "progress" for word in event["words"]]
    return supplied, words


def test_nominal_wav_is_bounded_by_its_inspected_frames(tmp_path, monkeypatch):
    path = tmp_path / "track-nominal.wav"
    write_wav(path, 16000)
    supplied, words = run_nominal(
        monkeypatch, dict(id="nominal", path=str(path), offsetMs=7250, sampleRate=16000, frames=12000)
    )
    assert supplied and set(supplied) == {8000}
    assert words and max(word["endMs"] for word in words) == 8000
    assert all(7250 <= word["startMs"] <= word["endMs"] <= 8000 for word in words)


def test_nominal_opus_is_bounded_by_decoded_pcm_not_source_metadata(tmp_path, monkeypatch):
    from cassini_talk import speech_pipeline as pipeline

    # One extra decoded Opus frame (20 ms) past the 1 s of source metadata;
    # publication also uses the decoded length.
    decoded = np.full(16320, 0.1, dtype="<f4")
    monkeypatch.setattr(pipeline, "audio_chunks", lambda track: iter([decoded]))
    monkeypatch.setattr(
        pipeline,
        "audio_segments",
        lambda track, segments, heartbeat=None: (decoded[s["startSample"] : s["endSample"]] for s in segments),
    )
    supplied, words = run_nominal(
        monkeypatch,
        dict(id="opus", path=str(tmp_path / "track.opus"), offsetMs=7250, sampleRate=48000, frames=48000, codec="opus"),
    )
    assert supplied and set(supplied) == {None}
    end = max(word["endMs"] for word in words)
    assert 8250 < end <= 7250 + 16320 * 1000 // 16000
