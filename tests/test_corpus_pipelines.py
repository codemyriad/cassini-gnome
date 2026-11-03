# SPDX-License-Identifier: GPL-3.0-or-later
"""D52 shared test 1 (G15): GNOME's own pipelines on the shared multistream corpus.

Each check_* takes what a GNOME pipeline produced and raises AssertionError when
it disagrees with the corpus's `expected` block and `constants`; no corpus time
is written here. The mutant tests at the end patch one rule and require the same
check to fail.

G10 on this corpus: with the VAD rejecting everything, only the microphone
reaches MIN_ACTIVE_SECONDS, so no remote span gets fallback words. The checks
pin what the fallback does produce; pooling per person is G10's own fix.
"""

import json
import math
import shutil
import wave
from contextlib import contextmanager
from datetime import datetime

import pytest
from corpus_support import (
    CALL,
    CORPUS,
    EXPECTED,
    OTHER,
    RATE,
    SESSIONS,
    C,
    copy_meeting,
    corpus,  # noqa: F401 (module fixture)
    expected,
    job_id,
    overlaps,
    person,
    run_pipeline,
    speaker,
    tracks,
)

np = pytest.importorskip("numpy")

# RunModel's 10 ms envelope block: a fake word may end up to one block past its audio.
GRAIN_MS = 10
# Probes stay this far from a span or gap edge, beyond the 97-tap FIR's 1 ms context.
EDGE_MS = 10
LSB = 1 / 32768


@pytest.fixture(autouse=True)
def release(tmp_path):
    """Later suite tests need their own free space on a small test tmpfs: drop these files."""
    yield
    shutil.rmtree(tmp_path.absolute(), ignore_errors=True)


def by_job(session=CALL):
    return {job_id(identifier): track for identifier, track in tracks(session).items()}


def clip(windows, ranges):
    """Half-open intersections of two sorted window lists."""
    return [[max(a, c), min(b, d)] for a, b in windows for c, d in ranges if max(a, c) < min(b, d)]


def total(windows):
    return sum(b - a for a, b in windows)


def union(windows):
    merged = []
    for a, b in sorted(windows):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def present_ms(identifier, session=CALL):
    """The track's owned presence in session ms, from expected presence16k."""
    return [[a * 1000 // RATE, b * 1000 // RATE] for a, b in expected(session)["tracks"][identifier]["presence16k"]]


def sounding_ms(identifier, *, bleeds=True, noise=False, session=CALL):
    """Owned presence where the corpus puts speech, bleeds and, with noise, the noise bed."""
    track = tracks(session)[identifier]
    windows = [w[:2] for w in track.get("speechWindows", [])]
    if bleeds:
        windows += [w for bleed in track.get("bleeds", []) for w in bleed["windows"]]
    if noise and "noise" in track:
        windows += track["noise"]["windows"]
    return clip(union(windows), present_ms(identifier, session))


def shrunk(word):
    """A fake word's own time, less the RunModel block it may round up to."""
    return word["startMs"], max(word["startMs"], word["endMs"] - GRAIN_MS)


# Scanner and rows ---------------------------------------------------------------------------------


def check_rows(snapshot):
    sessions = [SESSIONS[identifier] for identifier in EXPECTED["concurrency"]["sessions"]]
    meetings = {meeting.id: meeting for meeting in snapshot.meetings}
    # Newest first; the two concurrent calls stay two independent folders and rows.
    order = sorted(SESSIONS.values(), key=lambda session: session["createdAt"], reverse=True)
    assert [meeting.id for meeting in snapshot.meetings] == [session["id"] for session in order], "row order"
    assert len({meetings[session["id"]].directory for session in sessions}) == len(sessions), "one folder per call"
    starts = [meetings[session["id"]].started_at_ms for session in sessions]
    ends = [start + meetings[session["id"]].duration_ms for start, session in zip(starts, sessions, strict=True)]
    wall = [datetime.fromisoformat(value).timestamp() * 1000 for value in EXPECTED["concurrency"]["wallClock"]]
    assert [max(starts), min(ends)] == wall, "concurrent wall-clock window"
    assert min(ends) - max(starts) >= C["CONCURRENT_MIN_MS"], "the calls overlap"
    from cassini_talk import ux_formats

    for session in sessions:
        meeting = meetings[session["id"]]
        assert meeting.duration_ms == session["durationMs"], "duration"
        assert meeting.row_texts()[0] == ux_formats.row_meta(session["durationMs"], ()), "row meta"
        gapped = any(expected(session["id"])["tracks"][t]["gaps"] for t in tracks(session["id"]))
        badge = meeting.badge()
        # A stored gap range interrupts the call where it ends (D52.2).
        assert (badge.kind if badge else None) == ("interrupted" if gapped else None), "interrupted badge"
        assert meeting.interrupted_at_ms == (session["durationMs"] if gapped else None), "interrupted at"


def test_scanner_rows_keep_the_concurrent_calls_apart(corpus):  # noqa: F811
    from cassini_talk.library import LibraryScanner

    check_rows(LibraryScanner(corpus.root).scan())


# Track preparation: presence and ownership (G6, G8) -----------------------------------------------


def check_presence(jobs, meeting, session=CALL):
    rows = expected(session)["tracks"]
    corpus_tracks = by_job(session)
    assert set(jobs) == set(corpus_tracks), "one job per track"
    for name, job in jobs.items():
        track = corpus_tracks[name]
        assert "presence" in job, f"{track['id']} has no presence"
        presence = [
            [a + job["offsetMs"] * RATE // 1000, b + job["offsetMs"] * RATE // 1000] for a, b in job["presence"]
        ]
        assert presence == rows[track["id"]]["presence16k"], f"presence of {track['id']}"
    for track in meeting.tracks:
        windows = track.ownership_windows or ((0, None),)
        extent = tracks(session)[track.id]
        owned = [
            [max(a, extent["startMs"]), extent["endMs"] if b is None else min(b, extent["endMs"])] for a, b in windows
        ]
        assert [w for w in owned if w[0] < w[1]] == rows[track.id]["ownership"], f"ownership of {track.id}"


def test_prepared_jobs_carry_the_expected_presence_and_ownership(corpus):  # noqa: F811
    for session in (CALL, OTHER):
        check_presence(corpus.jobs[session], corpus.meetings[session], session)


# Capture record and details (G2, D47) -------------------------------------------------------------


def check_capture(meeting, session=CALL):
    from cassini_talk import capture_report, source_report

    rows = expected(session)
    stored = json.loads((meeting.directory / "session.json").read_text())
    record = capture_report.stored(meeting.metadata)
    assert record is not None, "captureRecord written"
    assert record["events"] == SESSIONS[session]["events"], "captureRecord events"
    assert {t["id"]: t["gaps"] for t in record["tracks"]} == {t["id"]: t["gaps"] for t in stored["tracks"]}, "gaps"
    outcome = {
        row["sourceTrackId"]: dict(captureOutcome=row["captureOutcome"], audioMs=row["audioMs"])
        for row in record["sources"]
    }
    assert outcome == rows["captureOutcome"], "captureOutcome sources"
    facts = {fact.identity: fact for fact in source_report.sources(stored, meeting.metadata, live=False)}
    assert set(facts) == set(rows["captureOutcome"]), "source rows"
    for identity, fact in facts.items():
        members = [t for t in tracks(session).values() if t.get("sourceTrackId") == identity]
        padding = sum(total(rows["tracks"][t["id"]]["gaps"]) for t in members)
        dropped = sum(rows["droppedFrames"].get(t["id"], 0) for t in members) * 1000 // CORPUS["rate"]
        assert fact.padding_ms == padding, f"gap time of {identity}"
        assert (fact.dropped_ms or 0) == dropped, f"dropped time of {identity}"
        # A dropped count equal to the padding is the same hole, shown once.
        assert len(fact.gap_lines) == bool(padding) + bool(dropped and dropped != padding), f"gap rows of {identity}"
        assert not fact.problem, f"{identity} was captured"
    from cassini_talk.capture_details import read_details
    from cassini_talk.strings import text

    report = read_details(meeting)
    health = dict(report.health)
    gapped = [g for t in tracks(session).values() for g in t.get("gaps", [])]
    if gapped:
        import locale

        assert health[text("details.health.problem")] == stored["problemKind"] == "gap", "problem kind"
        frames = locale.localize(str(sum(g["frames"] for g in gapped)), grouping=True)
        seconds = f"{sum(g['durationMs'] for g in gapped) / 1000:g} s"
        assert health[text("details.health.gaps")] == f"{frames} frames · {seconds}", "gap frames"
    assert [fact.identity for fact in report.sources] == [
        row["sourceTrackId"] for row in stored["captureOutcome"]["sources"]
    ]


def test_capture_record_and_details_report_the_corpus_sources(corpus, tmp_path):  # noqa: F811
    from cassini_talk.capture_report import record_capture
    from cassini_talk.library import LibraryScanner

    meeting = copy_meeting(corpus, tmp_path)
    before = {p.name: p.read_bytes() for p in meeting.directory.iterdir()}
    assert record_capture(meeting.directory, json.loads((meeting.directory / "session.json").read_text()))
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    check_capture(meeting)
    # D47: the capture record is metadata; the session and every WAV are unchanged.
    assert all((meeting.directory / name).read_bytes() == data for name, data in before.items())


# Recognition through the real child ---------------------------------------------------------------


@contextmanager
def pipeline_run(tmp_path_factory, name, **values):
    directory = tmp_path_factory.mktemp(name)
    # The fake child's patches end with its run, so a later run neither stacks on them nor reports into this one.
    with pytest.MonkeyPatch.context() as monkeypatch:
        run = run_pipeline(values.pop("corpus"), monkeypatch, directory, **values)
    yield run
    shutil.rmtree(directory.absolute(), ignore_errors=True)


@pytest.fixture(scope="module")
def reject_all(corpus, tmp_path_factory):  # noqa: F811
    """G10: a VAD that hears nothing, so only the VAD-free fallback can find words."""
    with pipeline_run(tmp_path_factory, "reject", corpus=corpus, probability=0.0) as run:
        yield run


@pytest.fixture(scope="module")
def accept_all(corpus, tmp_path_factory):  # noqa: F811
    with pipeline_run(tmp_path_factory, "accept", corpus=corpus, probability=1.0) as run:
        yield run


def overrun_model():
    from test_recognition_bounds import OverrunModel

    return OverrunModel()


@pytest.fixture(scope="module")
def overrun(corpus, tmp_path_factory):  # noqa: F811
    with pipeline_run(tmp_path_factory, "overrun", corpus=corpus, model=overrun_model) as run:
        yield run


def check_no_words(run, session=CALL):
    """No word in a gap or in time its track does not own (expected noWords), none outside presence.

    The gap ranges are digital zero in the WAVs, where RunModel never writes a
    word, so only the owned-time ranges that carry audio can fail here; the
    ownership-noWords mutant below shows they do.
    """
    rows = expected(session)["tracks"]
    for name, words in run.words.items():
        track = by_job(session)[name]
        for word in words:
            left, right = shrunk(word)
            assert not overlaps((left, right), rows[track["id"]]["noWords"]), f"{track['id']} word in {word}"
            inside = [w for w in present_ms(track["id"], session) if w[0] <= left and right <= w[1]]
            assert inside, f"{track['id']} word outside presence {word}"


def check_fallback(run, session=CALL):
    """G10 with the VAD rejecting everything: activity, floors and fallback words per track."""
    from cassini_talk import recognition_fallback as fallback

    rows = expected(session)["tracks"]
    breakdown = run.diagnostics("no-words-breakdown")
    used = run.diagnostics("few-words-fallback")
    assert breakdown, "no breakdown diagnostics"
    # Without VAD words, a track can only have words through the fallback, which reports its breakdown.
    assert set(run.words) <= set(breakdown), f"words without a breakdown: {set(run.words) - set(breakdown)}"
    floor_ms = fallback.FLOOR_FRAMES * fallback.FRAME * 1000 // fallback.RATE
    for name, row in breakdown.items():
        track = by_job(session)[name]
        # Steady noise and silence are never activity; speech and bleeds may be.
        assert row["activeSeconds"] * 1000 <= total(sounding_ms(track["id"], session=session)), f"{track['id']} active"
        # GNOME has a per-track floor only with FLOOR_FRAMES audible frames; compare there.
        audible = total(sounding_ms(track["id"], noise=True, session=session))
        assert (row["floorDb"] is not None) == (audible >= floor_ms), f"{track['id']} has a floor"
        if row["floorDb"] is not None:
            low, high = rows[track["id"]]["floorDb"]
            assert low <= row["floorDb"] <= high, f"{track['id']} floor {row['floorDb']} not in {low}..{high}"
        gate = row["activeSeconds"] >= fallback.MIN_ACTIVE_SECONDS
        assert (name in used) == gate, f"{track['id']} fallback gate"
        if gate:
            assert used[name]["used"] and used[name]["fallbackWords"] == len(run.words.get(name, [])) > 0, track["id"]
            assert used[name]["floorDb"] == row["floorDb"], f"{track['id']} one floor"
        else:
            assert not run.words.get(name), f"{track['id']} words without VAD or fallback"
    speaking = {person(t) for t in tracks(session).values() if run.words.get(job_id(t["id"]))}
    assert speaking <= {key for key, ms in expected(session)["speechMs"].items() if ms}, "who speaks"


def test_vad_rejecting_everything_falls_back_only_where_the_track_is_active(reject_all):
    check_fallback(reject_all)
    check_no_words(reject_all)
    fallen = {name for name, row in reject_all.diagnostics("few-words-fallback").items() if row["used"]}
    # G10 gap (not fixed here): no remote span reaches MIN_ACTIVE_SECONDS, so only my track falls back.
    assert {by_job()[name]["kind"] for name in fallen} == {"microphone"}


def test_the_fallback_check_fails_without_breakdown_diagnostics(reject_all):
    from dataclasses import replace

    with pytest.raises(AssertionError, match="no breakdown diagnostics"):
        check_fallback(replace(reject_all, summary=dict(reject_all.summary, diagnostics=[])))


def test_vad_accepting_everything_gives_words_only_in_owned_presence(accept_all):
    check_no_words(accept_all)
    speaking = {person(by_job()[name]) for name, words in accept_all.words.items() if words}
    assert speaking == {key for key, ms in expected()["speechMs"].items() if ms}


def shared_calls(session=CALL):
    """Per D49 or seam overlap: the groups of tracks of different people carrying one voice there."""
    rows = expected(session)
    ranges = [rows["d49Overlap"], *rows["seamOverlaps"]]
    assert ranges[0] and len(ranges) > 1
    result = []
    for left, right in ranges:
        voices = {}
        for track in tracks(session).values():
            if track["startMs"] <= left and right <= track["endMs"]:
                voices.setdefault(track["speech"], []).append(track)
        shared = [group for group in voices.values() if len({person(t) for t in group}) > 1]
        assert shared, f"no physical overlap at {left}"
        result.append(((left, right), shared))
    return result


def check_once(run, session=CALL):
    """D49 and the seams: the call captured twice is written down once."""
    for (left, right), shared in shared_calls(session):
        for group in shared:
            words = [
                [shrunk(w) for w in run.words.get(job_id(t["id"]), []) if overlaps(shrunk(w), [[left, right]])]
                for t in group
            ]
            assert any(words), f"nothing written at {left}"
            for i, first in enumerate(words):
                for second in words[i + 1 :]:
                    doubled = sum(max(0, min(b, d) - max(a, c)) for a, b in first for c, d in second)
                    assert doubled == 0, f"{left}..{right} written twice ({doubled} ms)"


def test_d49_and_seam_overlaps_are_written_down_once(accept_all):
    check_once(accept_all)


def check_clamp(run, session=CALL):
    """G11: an engine reporting words past its audio never places one outside its span."""
    for name, words in run.words.items():
        track = by_job(session)[name]
        assert words, name
        for word in words:
            assert track["startMs"] <= word["startMs"] <= word["endMs"] <= track["endMs"], (
                f"{track['id']} word outside its span {word}"
            )


def test_word_ends_are_clamped_to_their_span(overrun):
    check_clamp(overrun)


def check_turns(run, session=CALL):
    from cassini_talk.transcript import build_turns

    turns = build_turns(list(run.meeting.words))
    rows = expected(session)
    for overlap in rows["overlaps"]:
        window = overlap["ms"]
        people = [speaker(key) for key in overlap["people"]]
        for key in people:
            here = [t for t in turns if t.speaker == key and overlaps((t.start_ms, t.end_ms), [window])]
            assert here, f"no {key} turn at {window}"
            marked = set().union(*(t.over for t in here))
            assert set(people) - {key} <= marked, f"{key} at {window} overlaps {marked}"


def test_turns_overlap_where_people_talk_at_once(accept_all):
    check_turns(accept_all)


def check_rejoin(run, corpus, session=CALL):  # noqa: F811
    """Diarization input: each eligible group's rejoined audio is silent outside its presence."""
    from cassini_talk.diarization_audio import rejoin

    groups = corpus.groups[session]
    eligible = sorted(key for key, group in groups.items() if group["eligible"])
    # Each eligible group is rejoined once by this run, and only those.
    rejoined = sorted(speaker(person(by_job(session)[job_id(ids[0])])) for ids in run.rejoined)
    assert rejoined == eligible, "rejoined groups"
    for key, group in groups.items():
        if key == "me":
            continue
        members = [by_job(session)[name] for name in group["tracks"]]
        present = union([w for t in members for w in present_ms(t["id"], session)])
        speaking = union([w for t in members for w in sounding_ms(t["id"], bleeds=False, session=session)])
        jobs = [corpus.jobs[session][name] for name in group["tracks"]]
        with rejoin(jobs, scratch=run.meeting.directory.parent / f"scratch-{key}") as (stream, frames, _digest):
            samples = np.frombuffer(stream.read(), dtype="<f4")
        assert frames == group["durationMs"] * RATE // 1000, key
        edges = [0, *(edge for window in present for edge in window), group["durationMs"]]
        absent = [
            (a + EDGE_MS, b - EDGE_MS) for a, b in zip(edges[::2], edges[1::2], strict=True) if b - a > 2 * EDGE_MS
        ]
        assert absent, key
        for left, right in absent:
            assert not np.any(samples[left * RATE // 1000 : right * RATE // 1000]), f"{key} sound at {left}..{right}"
        for left, right in speaking:
            assert np.max(np.abs(samples[left * RATE // 1000 : right * RATE // 1000])) > 0.01, f"{key} quiet at {left}"


def test_rejoin_keeps_each_group_silent_outside_its_presence(accept_all, corpus):  # noqa: F811
    check_rejoin(accept_all, corpus)


def test_module_runs_leave_no_fake_child_patched_behind(reject_all, accept_all, overrun):
    import os
    import sys
    import types

    from cassini_talk import diarization_audio
    from cassini_talk import speech_pipeline as pipeline

    for value in (pipeline.SpeechDetector, pipeline.resampled_chunks, diarization_audio.rejoin):
        assert "<locals>" not in value.__qualname__, f"{value.__qualname__} still patched"
    assert not isinstance(sys.modules.get("cassini_talk.nemotron"), types.SimpleNamespace), "fake diarizer"
    for run in (reject_all, accept_all, overrun):
        assert os.environ.get("XDG_CACHE_HOME") != str(run.meeting.directory.parents[1] / "cache")


# Published mix and export (G8) --------------------------------------------------------------------


def pcm(path):
    with wave.open(str(path)) as reader:
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float64) / 32768


def published(meeting):
    """The Library v2 mixdown's PCM before the limiter, exactly as finalize builds it."""
    from cassini_talk.library_v2 import mixdown

    session = json.loads((meeting.directory / "session.json").read_text())
    timeline = mixdown.session_spans(meeting.directory, session)
    return np.concatenate(list(mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans)))), timeline


def owned_mix(meeting, session=CALL):
    """Every track's own PCM inside its expected owned presence, summed on the timeline."""
    scale = CORPUS["rate"] // RATE
    mix = np.zeros(SESSIONS[session]["durationMs"] * CORPUS["rate"] // 1000)
    for identifier, track in tracks(session).items():
        samples = pcm(meeting.directory / job_id(identifier))
        start = track["startMs"] * CORPUS["rate"] // 1000
        for a, b in expected(session)["tracks"][identifier]["presence16k"]:
            mix[a * scale : b * scale] += samples[a * scale - start : b * scale - start]
    return mix


def check_mix(mix, meeting, session=CALL):
    wanted = owned_mix(meeting, session)
    assert len(mix) == len(wanted), "published duration"
    worst = np.max(np.abs(mix - wanted))
    assert worst < LSB, f"published mix differs from the owned sum by {worst:.4f}"


def test_published_mix_and_export_hold_each_instant_once(corpus):  # noqa: F811
    from cassini_talk.export_audio import track_pcm
    from cassini_talk.source_ownership import presence, samples

    meeting = corpus.meetings[CALL]
    mix, timeline = published(meeting)
    check_mix(mix, meeting)
    # Export reads each track through the same mask; mixed_pcm sums these blocks
    # sample by sample in Python, so the sum here is numpy's.
    exported = np.zeros(len(mix))
    for track in meeting.tracks:
        values = np.concatenate([np.frombuffer(block, dtype="<f4") for block in track_pcm(track)])
        exported[: len(values)] += values
    assert np.max(np.abs(exported - mix)) < LSB
    # The x track map keeps every track and its gaps, unmasked (D47).
    session = json.loads((meeting.directory / "session.json").read_text())
    assert [t["id"] for t in timeline.tracks] == list(tracks())
    for entry, track in zip(timeline.tracks, session["tracks"], strict=True):
        assert entry["gaps"] == track["gaps"]
        assert samples(presence(entry).present) == samples(presence(track).present)


# B4 mutants: each patch must make its named check fail --------------------------------------------


def no_ownership_mask(monkeypatch):
    from cassini_talk.source_ownership import FrameMask

    monkeypatch.setattr(FrameMask, "sections", lambda self, first, count: [(0, count)])


def no_presence_mask(monkeypatch):
    from cassini_talk import transcription

    monkeypatch.setattr(transcription, "track_presence", lambda track: None)


def no_track_floor(monkeypatch):
    from cassini_talk.recognition_fallback import ACTIVE_DBFS, ActivityMeter

    monkeypatch.setattr(ActivityMeter, "threshold_db", lambda self: (None, ACTIVE_DBFS))


def no_word_clamp(monkeypatch):
    from cassini_talk import speech_pipeline as pipeline

    original = pipeline.recognise_segment

    def unclamped(model, path, segment, offset_ms=0, **kwargs):
        raw = []

        class Spy:
            def transcribe(self, samples, **values):
                result = model.transcribe(samples, **values)
                raw.extend(result.get("words", []))
                return result

        value, words, language, digest = original(Spy(), path, segment, offset_ms, **kwargs)
        base = math.floor(offset_ms) + segment["startSample"] * 1000 // RATE
        times = [
            dict(w, startMs=base + round(r["start"] * 1000), endMs=base + round(r["end"] * 1000))
            for w, r in zip(words, raw, strict=True)
        ]
        return value, times, language, digest

    monkeypatch.setattr(pipeline, "recognise_segment", unclamped)


def no_mix_ownership(monkeypatch):
    from cassini_talk.library_v2 import mixdown

    monkeypatch.setattr(mixdown, "windows", lambda kind, events: None)


def no_words_of(corpus, monkeypatch, tmp_path):  # noqa: F811
    # The tracks that own only part of their span, where noWords carries audio.
    partial = {i for i, t in tracks().items() if expected()["tracks"][i]["ownership"] != [[t["startMs"], t["endMs"]]]}
    check_no_words(run_pipeline(corpus, monkeypatch, tmp_path, keep=partial))


def mix_of(corpus, monkeypatch, tmp_path):  # noqa: F811
    meeting = corpus.meetings[CALL]
    mix, _timeline = published(meeting)
    check_mix(mix, meeting)


def presence_of(corpus, monkeypatch, tmp_path):  # noqa: F811
    from cassini_talk.transcription import prepare_tracks

    meeting = corpus.meetings[CALL]
    check_presence({job["id"]: job for job in prepare_tracks(meeting)[0]}, meeting)


def once_of(corpus, monkeypatch, tmp_path):  # noqa: F811
    keep = {t["id"] for _window, shared in shared_calls() for group in shared for t in group}
    check_once(run_pipeline(corpus, monkeypatch, tmp_path, keep=keep))


def fallback_of(corpus, monkeypatch, tmp_path):  # noqa: F811
    noisy = {t["id"] for t in tracks().values() if "noise" in t and t.get("sourceTrackId")}
    check_fallback(run_pipeline(corpus, monkeypatch, tmp_path, probability=0.0, keep=noisy))


def clamp_of(corpus, monkeypatch, tmp_path):  # noqa: F811
    remote = {t["id"] for t in tracks().values() if t.get("sourceTrackId")}
    check_clamp(run_pipeline(corpus, monkeypatch, tmp_path, model=overrun_model, keep=remote))


@pytest.mark.parametrize(
    ("mutant", "check", "message"),
    [
        (no_ownership_mask, once_of, "written twice"),
        (no_ownership_mask, no_words_of, " word in "),
        (no_ownership_mask, mix_of, "published mix differs"),
        (no_presence_mask, presence_of, "has no presence"),
        (no_track_floor, fallback_of, " (active|has a floor)"),
        (no_word_clamp, clamp_of, "word outside its span"),
        (no_mix_ownership, mix_of, "published mix differs"),
    ],
    ids=["ownership-words", "ownership-noWords", "ownership-mix", "presence", "track-floor", "clamp", "mix-ownership"],
)
def test_each_mutant_fails_its_named_check(corpus, monkeypatch, tmp_path, mutant, check, message):  # noqa: F811
    check(corpus, monkeypatch, tmp_path)  # the baseline passes
    mutant(monkeypatch)
    with pytest.raises(AssertionError, match=message):
        check(corpus, monkeypatch, tmp_path / "mutant")
