# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1120: a cached segment replaces only `model.transcribe`.

The cache is filled from a plain placement (offset 0, no presence, no end,
no ownership) and then read under each finalization placement. Every run
must publish byte-identical events to an uncached run of the same job.
"""

import json
import time
import wave

import pytest

np = pytest.importorskip("numpy")
from cassini_talk import recognition_cache as cache  # noqa: E402
from cassini_talk import speech_pipeline as pipeline  # noqa: E402

RATE = 16000
WHISTLE = "whistle@" + "1" * 64
PARAKEET = "parakeet-ultra-0.6b-int8-v2@" + "2" * 64


def detector(probability=None):
    class Detector:
        frame_size = 512
        runtime = dict(inputStage="fake")

        def __init__(self, *_):
            pass

        def probability(self, frame):
            if probability is not None:
                return probability
            return 1.0 if float(np.abs(frame).max()) > 0.01 else 0.0

        def reset(self):
            pass

    return Detector


def speech(seconds):
    """300 Hz bursts on even seconds (a 0.2 s quiet onset, then 0.8 s loud); odd seconds are silent.

    Identical bursts give identical segment PCM, which one job also recognises once.
    """
    t = np.arange(seconds * RATE) / RATE
    samples = 0.25 * np.sin(2 * np.pi * 300 * t)
    for second in range(seconds):
        if second % 2:
            samples[second * RATE : (second + 1) * RATE] = 0
        else:
            samples[second * RATE : second * RATE + RATE // 5] *= 0.02 / 0.25
    return samples


def write(path, samples):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, RATE, 0, "NONE", ""))
        stream.writeframes(np.round(np.asarray(samples) * 32767).astype("<i2").tobytes())
    return path


class RunModel:
    """One word per contiguous nonzero run; the last word overruns the PCM (G11)."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, samples, language, word_timestamps):
        assert language is None and word_timestamps is True
        self.calls += 1
        block = 160
        envelope = np.abs(samples[: len(samples) // block * block]).reshape(-1, block).max(axis=1) > 0.01
        words, start = [], None
        for index, on in enumerate(np.append(envelope, False)):
            if on and start is None:
                start = index
            elif not on and start is not None:
                words.append(dict(word=f"w{len(words)}", start=start * block / RATE, end=index * block / RATE))
                start = None
        if words:
            words[-1]["end"] = len(samples) / RATE + 0.75
        return dict(text=" ".join(w["word"] for w in words), language="en", words=words)


class Forbidden:
    def transcribe(self, *_args, **_kw):
        raise AssertionError("a cached segment ran the recogniser")


def run(monkeypatch, model, tracks, probability=None, cache_options=None):
    monkeypatch.setattr(pipeline, "SpeechDetector", detector(probability))
    events = []
    result = pipeline.process_tracks(
        model,
        tracks,
        None,
        lambda kind, **values: events.append(dict(type=kind, **values)),
        recognition_cache=cache_options,
    )
    # ETA and speed are measurements, not output.
    published = [
        {k: v for k, v in e.items() if k not in ("etaMinutes", "realtimeFactor")}
        for e in events
        if e["type"] == "progress"
    ]
    entries = {
        e["cacheEntry"]["key"]: {k: e["cacheEntry"][k] for k in ("text", "language", "words")}
        for e in events
        if "cacheEntry" in e
    }
    return json.dumps(dict(result=result, events=published), sort_keys=True), entries


def warm(monkeypatch, tmp_path, path, probability=None, model=WHISTLE):
    """Fill the cache from a plain placement and store it as the queue does."""
    cache_file = tmp_path / cache.FILE
    _output, entries = run(
        monkeypatch,
        RunModel(),
        [dict(id="t", path=str(path), offsetMs=0)],
        probability,
        dict(path=str(cache_file), model=model),
    )
    assert entries and all(cache.valid_entry(k, v) for k, v in entries.items())
    cache_file.write_text(json.dumps(cache.document(model, entries)))
    return cache_file, entries


SCENARIOS = {
    # D55: words are placed after Stop from the final offset.
    "offset": (12, None, dict(offsetMs=2500)),
    "fractional-offset": (12, None, dict(offsetMs=1234.5)),
    # D55 M9: an unknown offset is placed at an assumed 0 and reported.
    "unknown-offset": (12, None, dict(offsetMs=None)),
    # G11: the track's published end clamps every word placed past it.
    "audio-end": (12, None, dict(offsetMs=700, frames=9 * RATE, sampleRate=RATE)),
    # D63 #3: only words whose midpoint is present are written.
    "presence": (12, None, dict(offsetMs=0, sessionPresence=[[0, 5 * RATE], [8 * RATE + RATE // 2, 30 * RATE]])),
    # R71: VAD finds nothing, so VAD-free windows (`noVadWindow`) replace it.
    "fallback": (40, 0.0, dict(offsetMs=1000)),
    "fallback-presence": (60, 0.0, dict(offsetMs=0, sessionPresence=[[0, 44 * RATE + RATE // 2]])),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_cache_hits_publish_byte_identical_output_under_every_placement(tmp_path, monkeypatch, name):
    seconds, probability, extra = SCENARIOS[name]
    path = write(tmp_path / "track.wav", speech(seconds))
    job = [dict(id="t", path=str(path), **extra)]
    baseline_model = RunModel()
    baseline, _ = run(monkeypatch, baseline_model, job, probability)
    assert baseline_model.calls
    cache_file, _entries = warm(monkeypatch, tmp_path, path, probability)
    reused, missed = run(monkeypatch, Forbidden(), job, probability, dict(path=str(cache_file), model=WHISTLE))
    assert reused == baseline
    assert missed == {}, "a hit is never offered again"
    if name.startswith("fallback"):
        assert '"noVadWindow"' in baseline
    if name == "unknown-offset":
        assert "unknown-offset-display-zero" in baseline
    if name == "presence":
        assert (
            json.loads(baseline)["events"]
            != json.loads(run(monkeypatch, RunModel(), [dict(job[0], sessionPresence=None)])[0])["events"]
        )


def test_ownership_masking_reuses_only_identical_segment_pcm(tmp_path, monkeypatch):
    path = write(tmp_path / "track.wav", speech(12))
    # Ownership ends at 6 s: later bursts are masked to silence, earlier PCM is untouched.
    job = [dict(id="t", path=str(path), offsetMs=0, ownershipWindows=[[0, 6000]])]
    baseline_model = RunModel()
    baseline, _ = run(monkeypatch, baseline_model, job)
    cache_file, entries = warm(monkeypatch, tmp_path, path)
    model = RunModel()
    reused, missed = run(monkeypatch, model, job, cache_options=dict(path=str(cache_file), model=WHISTLE))
    assert reused == baseline
    assert model.calls == 0 and not missed and baseline_model.calls
    unmasked, _ = run(monkeypatch, RunModel(), [dict(job[0], ownershipWindows=None)])
    assert json.loads(unmasked)["events"] != json.loads(baseline)["events"], "the mask removed later bursts"


@pytest.mark.parametrize("other", [PARAKEET, "whistle@" + "3" * 64])
def test_model_switch_recomputes_everything(tmp_path, monkeypatch, other):
    path = write(tmp_path / "track.wav", speech(12))
    job = [dict(id="t", path=str(path), offsetMs=300)]
    baseline_model = RunModel()
    baseline, _ = run(monkeypatch, baseline_model, job)
    cache_file, entries = warm(monkeypatch, tmp_path, path, model=WHISTLE)
    model = RunModel()
    output, offered = run(monkeypatch, model, job, cache_options=dict(path=str(cache_file), model=other))
    # Within one job a repeated segment PCM is recognised once, cache file or not.
    assert output == baseline and model.calls == len(entries) <= baseline_model.calls
    assert set(offered) == set(entries), "the new model's own output is offered under its header"


def test_hundred_ppm_drifted_pcm_never_hits(tmp_path, monkeypatch):
    nominal = speech(12)
    position = np.arange(round(len(nominal) * 1.0001)) / 1.0001
    drifted = np.interp(position, np.arange(len(nominal)), nominal)
    path = write(tmp_path / "nominal.wav", nominal)
    cache_file, entries = warm(monkeypatch, tmp_path, path)
    drifted_path = write(tmp_path / "drifted.wav", drifted)
    job = [dict(id="t", path=str(drifted_path), offsetMs=0)]
    baseline_model = RunModel()
    baseline, _ = run(monkeypatch, baseline_model, job)
    model = RunModel()
    output, offered = run(monkeypatch, model, job, cache_options=dict(path=str(cache_file), model=WHISTLE))
    assert output == baseline and model.calls == len(offered) > 0
    assert not set(offered) & set(entries)


@pytest.mark.parametrize("damage", ["truncated", "corrupted", "v1", "bad-timestamps"])
def test_damaged_cache_recomputes_safely(tmp_path, monkeypatch, damage):
    path = write(tmp_path / "track.wav", speech(12))
    job = [dict(id="t", path=str(path), offsetMs=500)]
    baseline_model = RunModel()
    baseline, _ = run(monkeypatch, baseline_model, job)
    cache_file, entries = warm(monkeypatch, tmp_path, path)
    raw = cache_file.read_bytes()
    if damage == "truncated":
        cache_file.write_bytes(raw[: len(raw) // 2])
    elif damage == "corrupted":
        cache_file.write_bytes(raw.replace(b'"segments"', b'"segments\x00"', 1))
    elif damage == "v1":
        cache_file.write_bytes(raw.replace(cache.FORMAT.encode(), b"cassini.recognition-cache.v1"))
    else:
        broken = {
            key: dict(value, words=[dict(w, start=-1.0) for w in value["words"]]) for key, value in entries.items()
        }
        cache_file.write_text(json.dumps(cache.document(WHISTLE, broken)))
    model = RunModel()
    output, offered = run(monkeypatch, model, job, cache_options=dict(path=str(cache_file), model=WHISTLE))
    assert output == baseline and model.calls == len(entries)
    assert offered == entries


def test_invalid_recogniser_output_is_used_but_never_cached(tmp_path, monkeypatch):
    path = write(tmp_path / "track.wav", speech(4))

    class Backwards(RunModel):
        def transcribe(self, samples, language, word_timestamps):
            result = super().transcribe(samples, language, word_timestamps)
            for word in result["words"]:
                word["start"], word["end"] = word["end"], word["start"]
            return result

    output, offered = run(
        monkeypatch,
        Backwards(),
        [dict(id="t", path=str(path), offsetMs=0)],
        cache_options=dict(path=str(tmp_path / cache.FILE), model=WHISTLE),
    )
    assert json.loads(output)["events"] and offered == {}


def test_eleven_hour_plan_fits_and_scales_linearly():
    """BUG-CLASSES #1/#17: an 11 h meeting's cache stays bounded and linear (thread CPU, D88)."""

    def plan(hours):
        segments = {}
        for index in range(hours * 3600 // 5):
            digest = f"{index:064x}"
            words = [dict(start=0.37 * n, end=0.37 * n + 0.3, text=f"word{n}") for n in range(12)]
            segments[cache.segment_key(digest, 5 * RATE)] = dict(text="x", language="en", words=words)
        return json.dumps(cache.document(WHISTLE, segments)).encode()

    def cpu(raw):
        started = time.thread_time()
        segments = cache.parse(raw, WHISTLE)
        placed = 0
        for index, raw_entry in enumerate(segments.values()):
            segment = dict(startSample=index * 5 * RATE, endSample=(index + 1) * 5 * RATE)
            _value, words, _language = pipeline.place_words(raw_entry, segment, 5 * RATE, 1234.5, None)
            placed += len(pipeline.present_words(words, [[0, 12 * 3600 * RATE]]))
        return time.thread_time() - started, len(segments), placed

    half, full = plan(5), plan(11)
    assert len(full) < cache.MAX_BYTES, "an 11 h cache must not exceed the read bound"
    cpu(half)
    half_cpu, half_segments, _ = cpu(half)
    full_cpu, full_segments, placed = cpu(full)
    assert full_segments == 11 * 720 and placed == full_segments * 12 and half_segments == 5 * 720
    assert full_cpu < max(half_cpu, 0.01) * 2.2 * 2
    assert full_cpu < 10
