# SPDX-License-Identifier: GPL-3.0-or-later
"""G10: a track whose VAD pass finds too few words gets a VAD-free windowed decode."""

import wave

import pytest

np = pytest.importorskip("numpy")
from cassini_talk import recognition_fallback as fallback  # noqa: E402
from cassini_talk import speech_pipeline as pipeline  # noqa: E402

RATE = 16000


def detector(probability):
    class Detector:
        frame_size = 512
        runtime = {}

        def __init__(self, *_):
            pass

        def probability(self, _frame):
            return probability

        def reset(self):
            pass

    return Detector


def write(path, samples):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, RATE, 0, "NONE", ""))
        stream.writeframes(np.round(np.asarray(samples) * 32767).astype("<i2").tobytes())
    return path


def tone(seconds, level=0.25):
    t = np.arange(int(seconds * RATE)) / RATE
    return level * np.sin(2 * np.pi * 300 * t)


def speech(seconds, *, silent=(), dc=0.0):
    """Speech-like bursts on even seconds: 0.2 s quiet onset, then 0.8 s loud.

    Odd seconds, and the half-open second ranges in silent, are digital silence.
    """
    samples = tone(seconds)
    for second in range(seconds):
        a, b = second * RATE, (second + 1) * RATE
        if second % 2 or any(left <= second < right for left, right in silent):
            samples[a:b] = 0
        else:
            samples[a : a + RATE // 5] *= 0.02 / 0.25
    return samples + dc


class RunModel:
    """One word per contiguous nonzero run, at the run's own window-relative time."""

    def __init__(self):
        self.calls = []

    def transcribe(self, samples, language, word_timestamps):
        self.calls.append(len(samples))
        block = 160  # a 10 ms envelope, so zero crossings do not split a run
        envelope = np.abs(samples[: len(samples) // block * block]).reshape(-1, block).max(axis=1) > 0.01
        words, start = [], None
        for index, on in enumerate(np.append(envelope, False)):
            if on and start is None:
                start = index
            elif not on and start is not None:
                words.append(dict(word="speech", start=start * block / RATE, end=index * block / RATE))
                start = None
        return dict(text=" ".join(w["word"] for w in words), language="en", words=words)


class ScriptModel:
    """Returns a scripted number of distinct words per call, one second apart."""

    def __init__(self, counts):
        self.counts, self.calls = list(counts), 0

    def transcribe(self, samples, language, word_timestamps):
        count = self.counts[self.calls] if self.calls < len(self.counts) else 0
        self.calls += 1
        words = [dict(word=f"w{self.calls}-{n}", start=1.0 + n, end=1.4 + n) for n in range(count)]
        return dict(text=" ".join(w["word"] for w in words), language="en", words=words)


def run(tmp_path, monkeypatch, samples, model, probability, offset=0):
    monkeypatch.setattr(pipeline, "SpeechDetector", detector(probability))
    path = write(tmp_path / "track.wav", samples)
    events = []
    result = pipeline.process_tracks(
        model,
        [dict(id="one", path=str(path), offsetMs=offset, frames=len(samples), sampleRate=RATE)],
        None,
        lambda kind, **values: events.append(dict(type=kind, **values)),
    )
    progress = [event for event in events if event["type"] == "progress"]
    return result, progress, [w for event in progress for w in event["words"]]


def diagnostic(result, code):
    return next((d for d in result["diagnostics"] if d["code"] == code), None)


def test_vad_rejecting_everything_falls_back_to_windows_without_duplicating_the_seam(tmp_path, monkeypatch):
    model = RunModel()
    result, progress, words = run(tmp_path, monkeypatch, speech(40), model, 0.0, offset=1000)
    assert result["segments"] == 0
    assert [(e["segment"]["startSample"], e["segment"]["endSample"]) for e in progress] == [
        (0, 240000),
        (232000, 472000),
        (464000, 640000),
    ]
    assert all(e["segment"]["reason"] == "noVadWindow" for e in progress)
    # One word per burst, including the burst at 14-15 s inside the first seam.
    starts = [w["startMs"] for w in words]
    assert starts == [1000 + 2000 * n for n in range(20)], starts
    assert all(w["endMs"] <= 1000 + 40000 for w in words)
    used = diagnostic(result, "few-words-fallback")
    assert used["used"] and used["vadWords"] == 0 and used["fallbackWords"] == 20 and used["windows"] == 3
    assert used["floorDb"] == -37 and used["thresholdDb"] == -25
    assert diagnostic(result, "no-words-breakdown")["vadSegments"] == 0


def test_mutation_disabling_the_fallback_loses_every_word(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "MIN_ACTIVE_SECONDS", float("inf"))
    _result, progress, words = run(tmp_path, monkeypatch, speech(40), RunModel(), 0.0)
    assert not progress and not words


def test_skipped_quiet_windows_keep_later_windows_on_their_own_audio(tmp_path, monkeypatch):
    model = RunModel()
    samples = speech(64, silent=[(24, 50)])
    result, progress, words = run(tmp_path, monkeypatch, samples, model, 0.0)
    # [29 s, 44 s) is silent and skipped; the reader must still find 43.5 s and 58 s.
    assert [e["segment"]["startSample"] for e in progress] == [0, 232000, 696000, 928000]
    assert [w["startMs"] for w in words] == [2000 * n for n in range(12)] + [2000 * n for n in range(25, 32)]
    assert diagnostic(result, "few-words-fallback")["windows"] == 4


@pytest.mark.parametrize("name", ["zeros", "dc"])
def test_inactive_audio_never_runs_the_fallback(tmp_path, monkeypatch, name):
    samples = np.zeros(32 * RATE) if name == "zeros" else np.full(32 * RATE, -2060 / 32768)
    model = RunModel()
    result, progress, _words = run(tmp_path, monkeypatch, samples, model, 0.0)
    assert not model.calls and not progress
    assert diagnostic(result, "few-words-fallback") is None
    assert diagnostic(result, "no-words-breakdown")["activeSeconds"] == 0


def test_short_active_audio_streams_vad_events_unheld(tmp_path, monkeypatch):
    model = ScriptModel([1])
    result, progress, words = run(tmp_path, monkeypatch, speech(10), model, 1.0)
    assert model.calls == 1 and len(words) == 1 and diagnostic(result, "few-words-fallback") is None


@pytest.mark.parametrize(
    "vad,fallback_counts,replaced",
    [
        ([2, 2, 1], [3, 2, 1], False),  # 5 VAD words need 7: 6 keep the VAD words
        ([2, 2, 1], [3, 3, 1], True),  # 7 replace them
        ([0, 0, 0], [1, 0, 0], True),  # no VAD words: one fallback word wins
    ],
)
def test_fallback_replaces_only_past_the_margin_and_never_adds(tmp_path, monkeypatch, vad, fallback_counts, replaced):
    model = ScriptModel(vad + fallback_counts)
    result, progress, words = run(tmp_path, monkeypatch, speech(40), model, 1.0)
    assert model.calls == 6
    reasons = {e["segment"]["reason"] for e in progress}
    assert (reasons == {"noVadWindow"}) is replaced
    assert len(words) == (sum(fallback_counts) if replaced else sum(vad))
    assert diagnostic(result, "few-words-fallback")["used"] is replaced


def test_ten_vad_words_never_fire_and_keep_the_event_sequence(tmp_path, monkeypatch):
    model = ScriptModel([4, 4, 2])
    result, held, _words = run(tmp_path, monkeypatch, speech(40), model, 1.0)
    assert model.calls == 3 and diagnostic(result, "few-words-fallback") is None
    monkeypatch.setattr(fallback, "MIN_ACTIVE_SECONDS", float("inf"))
    _result, streamed, _words = run(tmp_path, monkeypatch, speech(40), ScriptModel([4, 4, 2]), 1.0)
    strip = [{k: v for k, v in e.items() if k not in ("etaMinutes", "realtimeFactor")} for e in held]
    assert strip == [{k: v for k, v in e.items() if k not in ("etaMinutes", "realtimeFactor")} for e in streamed]


@pytest.mark.parametrize(
    "windows",
    [
        fallback.window_bounds(40 * RATE),
        [(0, 240000), (464000, 640000)],
        [(400000, 640000)],
        [(232000, 472000), (600000, 640000)],
    ],
    ids=["contiguous", "gap", "late-first", "late-and-gap"],
)
def test_window_reader_matches_slices_of_the_whole_track(windows):
    samples = np.arange(40 * RATE, dtype="<f4")
    blocks = iter(np.array_split(samples, 97))
    for (start, end), value in zip(windows, fallback.audio_windows(blocks, windows), strict=True):
        assert np.array_equal(value, samples[start:end])


def meter_of(samples):
    meter = fallback.ActivityMeter()
    list(meter.wrap([np.asarray(samples, dtype="<f4")]))
    return meter


def test_activity_floor_comes_from_audible_frames_only():
    meter = meter_of(speech(40))
    assert meter.threshold_db() == (-37.0, -25.0)
    assert 16 <= meter.active_seconds() <= 17
    # Too few audible frames for a floor: -50 dBFS applies alone.
    sparse = meter_of(speech(32, silent=[(4, 32)]))
    assert sparse.threshold_db() == (None, -50.0)
    assert fallback.active_windows(sparse.samples, sparse.active()) == [(0, 240000)]


@pytest.mark.parametrize("zeros", [0, 12], ids=["continuous", "with-silence"])
def test_steady_noise_never_runs_the_fallback_however_much_silence_it_has(tmp_path, monkeypatch, zeros):
    noise = np.random.default_rng(1).standard_normal(40 * RATE) * 0.0056  # about -45 dBFS
    noise[: zeros * RATE] = 0
    model = RunModel()
    result, _progress, _words = run(tmp_path, monkeypatch, noise, model, 0.0)
    assert not model.calls and diagnostic(result, "few-words-fallback") is None
    assert meter_of(noise).threshold_db() == (-45.0, -33.0)


def test_a_dc_offset_is_removed_before_measuring_activity(tmp_path, monkeypatch):
    result, _progress, _words = run(tmp_path, monkeypatch, speech(40, dc=0.1), RunModel(), 0.0)
    decided = diagnostic(result, "few-words-fallback")
    assert decided["activeSeconds"] == meter_of(speech(40)).active_seconds()
    assert (decided["floorDb"], decided["thresholdDb"]) == (-37, -25)


def test_isolated_clicks_are_not_speech(tmp_path, monkeypatch):
    clicks = np.random.default_rng(2).standard_normal(40 * RATE) * 0.001
    for start in range(0, 40 * RATE, 1024):
        clicks[start : start + 80] = 0.5
    model = RunModel()
    result, _progress, _words = run(tmp_path, monkeypatch, clicks, model, 0.0)
    assert not model.calls and diagnostic(result, "few-words-fallback") is None


def test_held_decodes_keep_sending_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "SpeechDetector", detector(1.0))
    path = write(tmp_path / "track.wav", speech(40))
    events = []
    pipeline.process_tracks(
        ScriptModel([1, 1, 1, 1, 1, 1]),
        [dict(id="one", path=str(path), offsetMs=0, frames=40 * RATE, sampleRate=RATE)],
        None,
        lambda kind, **values: events.append(kind),
    )
    after = events[events.index("planned") + 1 :]
    assert after.count("planning") >= 6, "one per held VAD decode and per fallback window"


def test_energy_gate_drops_words_over_silence_and_a_dc_only_window():
    # Window at 1000 ms: one second of tone, then two seconds of digital silence.
    samples = np.concatenate((np.asarray(tone(1), dtype="<f4"), np.zeros(2 * RATE, dtype="<f4")))
    words = [word("speech", 1300, 1600), word("edge", 2050, 2100), word("silence", 2400, 2900)]
    assert [w["text"] for w in fallback.energetic(words, samples, 1000)] == ["speech", "edge"]
    dc = np.full(3 * RATE, 0.3, dtype="<f4")
    assert fallback.energetic(words, dc, 1000) == [], "a constant offset is not energy"


def word(text, start, end):
    return dict(text=text, startMs=start, endMs=end)


def test_seam_keeps_one_copy_of_a_confident_duplicate_from_the_side_with_more_context():
    previous = [word("hello", 13800, 14200), word("world", 14560, 14900)]
    following = [word("World.", 14600, 14950), word("again", 15200, 15500)]
    merged = fallback.merge_seam(previous, following, 14500, 500)
    assert [w["text"] for w in merged] == ["hello", "World.", "again"]


def test_seam_prefers_a_timed_copy_and_accepts_a_consistent_wider_shift():
    previous = [word("one", 14520, 14520), word("two", 14700, 14700)]
    following = [word("one", 14850, 15000), word("two", 15030, 15200)]
    merged = fallback.merge_seam(previous, following, 14500, 500)
    assert merged == following, "a 330 ms run shift is consistent, and timed copies win"


def test_seam_disagreement_cuts_at_the_overlap_midpoint():
    previous = [word("before", 14000, 14300), word("cat", 14600, 14800)]
    following = [word("dog", 14550, 14700), word("hat", 14800, 14950), word("after", 15100, 15300)]
    merged = fallback.merge_seam(previous, following, 14500, 500)
    assert [w["text"] for w in merged] == ["before", "cat", "hat", "after"], "starts own each side of 14750"


def test_seam_does_not_trust_a_lone_match_beyond_two_hundred_ms():
    previous = [word("yes", 14550, 14700)]
    following = [word("yes", 14900, 15000)]
    merged = fallback.merge_seam(previous, following, 14500, 500)
    # As in gocassini: two quick "yes" may be real, so the midpoint keeps each side's own.
    assert merged == previous + following


def test_margin():
    assert [fallback.need(n) for n in (0, 1, 5, 9, 20)] == [1, 3, 7, 11, 24]


def test_the_summary_reports_each_gated_track_for_provenance(tmp_path, monkeypatch):
    result, _progress, _words = run(tmp_path, monkeypatch, speech(40), RunModel(), 0.0)
    assert result["fallback"] == dict(
        method="vadFreeWindows",
        windowMs=15000,
        overlapMs=500,
        tracks=[dict(trackId="one", vadWords=0, fallbackWords=20, activeSeconds=16.64, used=True)],
    )
    quiet, _progress, _words = run(tmp_path, monkeypatch, speech(10), ScriptModel([1]), 1.0)
    assert "fallback" not in quiet
