# SPDX-License-Identifier: GPL-3.0-or-later
"""D52 shared test 1 (G15): GNOME's own pipelines on the shared multistream corpus.

Each check_* takes what a GNOME pipeline produced and raises AssertionError when
it disagrees with the corpus's `expected` block and `constants`; no corpus time
is written here. The mutant tests at the end patch one rule and require the same
check to fail.

G10 on this corpus (D63 #1): with the VAD rejecting everything, the speakers
in expected.fallback fall back. Me does in session 1. source-d does in session 3,
whose two spans each fall short of MIN_ACTIVE_SECONDS but not pooled; source-b's
pooled band stays under it.
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
    RATE,
    REJOINED,
    SESSIONS,
    C,
    Mute,
    copy_meeting,
    corpus,  # noqa: F401 (module fixture)
    expected,
    job_id,
    label,
    overlaps,
    person,
    run_pipeline,
    scripted,
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

    for session in order:
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
        # D63 #3 compares in session samples; the child's meter shifts the same ranges.
        assert "presence" not in job, "presence is sent once"
        assert "sessionPresence" in job, f"{track['id']} has no presence"
        assert job["sessionPresence"] == rows[track["id"]]["presence16k"], f"presence of {track['id']}"
    for track in meeting.tracks:
        windows = track.ownership_windows or ((0, None),)
        extent = tracks(session)[track.id]
        owned = [
            [max(a, extent["startMs"]), extent["endMs"] if b is None else min(b, extent["endMs"])] for a, b in windows
        ]
        assert [w for w in owned if w[0] < w[1]] == rows[track.id]["ownership"], f"ownership of {track.id}"


def test_prepared_jobs_carry_the_expected_presence_and_ownership(corpus):  # noqa: F811
    for session in SESSIONS:
        check_presence(corpus.jobs[session], corpus.meetings[session], session)


# Activity and the fallback decision (G10, D63 #1) -------------------------------------------------


def oracle():
    """The pinned independent activity oracle (stdlib only)."""
    import importlib.util

    from corpus_support import ROOT

    spec = importlib.util.spec_from_file_location("shared_activity", ROOT / "activity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_constants(fallback, shared):
    """GNOME's G10 constants against corpus.json and the oracle, in frames and in time."""
    from fractions import Fraction

    assert (fallback.RATE, fallback.FRAME) == (RATE, 512) and shared.FRAME_16K == fallback.FRAME, "frame"
    assert shared.FRAME == fallback.FRAME * shared.HOLD, "frame at 48 kHz"
    assert Fraction(fallback.FRAME * 1000, fallback.RATE) == shared.FRAME_MS == 32, "frame ms"
    assert fallback.FLOOR_FRAMES == C["FLOOR_MIN_FRAMES"] == shared.FLOOR_MIN_FRAMES == 312, "floor frames"
    assert Fraction(fallback.FLOOR_FRAMES * fallback.FRAME, fallback.RATE) == Fraction("9.984"), "floor time"
    assert fallback.MIN_RUN_FRAMES == shared.MIN_RUN_FRAMES == C["MIN_RUN_MS"] // shared.FRAME_MS == 5, "run"
    assert fallback.MIN_ACTIVE_SECONDS * 1000 == C["MIN_ACTIVE_SECONDS"] * 1000 == shared.MIN_ACTIVE_MS, "active"
    boundary = -(-shared.MIN_ACTIVE_MS // shared.FRAME_MS)
    assert boundary == 469 and fallback.holds(boundary) and not fallback.holds(boundary - 1), "pooled boundary"
    assert fallback.FEW_WORDS == C["FEW_WORDS"] == shared.FEW_WORDS, "few words"
    assert fallback.ACTIVE_DBFS == C["ACTIVE_DBFS"] == shared.ACTIVE_DBFS, "active dBFS"
    assert fallback.FLOOR_MARGIN_DB == C["FLOOR_MARGIN_DB"] == shared.FLOOR_MARGIN_DB, "floor margin"
    assert fallback.FLOOR_PERCENTILE == shared.PERCENTILE, "floor percentile"
    assert fallback.WINDOW == C["WINDOW_MS"] * RATE // 1000 == shared.WINDOW, "window"
    assert fallback.OVERLAP == C["OVERLAP_MS"] * RATE // 1000 == shared.OVERLAP, "overlap"
    assert Fraction(str(fallback.MIN_WINDOW_ACTIVE_SECONDS)) * 1000 == C["MIN_WINDOW_ACTIVE_MS"], "window active"
    needed = math.ceil(fallback.MIN_WINDOW_ACTIVE_SECONDS * fallback.RATE / fallback.FRAME)
    assert needed == shared.MIN_WINDOW_FRAMES == 16, "window active frames"


def test_gnome_fallback_constants_match_the_corpus_and_the_oracle():
    from cassini_talk import recognition_fallback as fallback

    check_constants(fallback, oracle())


def metered(job):
    """GNOME's ActivityMeter on one prepared job, fed the child's own FIR chunks."""
    from cassini_talk import recognition_fallback as fallback
    from cassini_talk.speech_pipeline import audio_chunks

    meter = fallback.ActivityMeter(fallback.track_ranges(job.get("sessionPresence"), job["offsetMs"]))
    for _block in meter.wrap(audio_chunks(job)):
        pass
    return meter


def within(value, band):
    return band[0] <= value <= band[1]


def check_activity(jobs, session=CALL):
    """Per track: activeSeconds in its band, the windows exactly; per person: the pooled band and decision."""
    from cassini_talk import recognition_fallback as fallback

    rows = expected(session)
    facts = []
    for name, job in jobs.items():
        track = by_job(session)[name]
        meter = metered(job)
        active = meter.active()
        seconds = meter.active_seconds(active)
        assert within(seconds, rows["tracks"][track["id"]]["activeSeconds"]), f"{track['id']} active {seconds}"
        windows = [
            [job["offsetMs"] + a * 1000 // RATE, job["offsetMs"] + b * 1000 // RATE]
            for a, b in fallback.active_windows(meter.samples, active, meter.presence)
        ]
        assert windows == rows["tracks"][track["id"]]["fallbackWindowsMs"], f"{track['id']} windows {windows}"
        facts.append(
            dict(trackId=track["id"], speaker=job["speaker"], activeFrames=sum(active), vadWords=0, fallbackWords=0)
        )
    pooled = fallback.pooled(facts)
    for key, band in rows["personActiveSeconds"].items():
        value = fallback.seconds(pooled[speaker(key)][0])
        assert within(value, band), f"{key} pooled active {value}"
    verdict, _entries = fallback.decide(facts)
    assert verdict == {speaker(key): value for key, value in rows["fallback"].items()}, "fallback per person"
    return facts


def test_activity_windows_and_the_pooled_decision_match_the_vectors(corpus):  # noqa: F811
    from cassini_talk import recognition_fallback as fallback

    only_pooled = set()
    for session in SESSIONS:
        facts = check_activity(corpus.jobs[session], session)
        for key, value in expected(session)["fallback"].items():
            mine = [f for f in facts if f["speaker"] == speaker(key)]
            if value and not any(fallback.holds(f["activeFrames"]) for f in mine):
                only_pooled.add((session, key))
    # Neither span of source-d reaches the gate alone; my session 1 track does.
    assert only_pooled == {(REJOINED, "source-d")}


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
def pipeline_run(tmp_path_factory, name, script=None, **values):
    directory = tmp_path_factory.mktemp(name)
    # The fake child's patches end with its run, so a later run neither stacks on them nor reports into this one.
    with pytest.MonkeyPatch.context() as monkeypatch:
        if script is not None:
            scripted(monkeypatch, script)
        run = run_pipeline(values.pop("corpus"), monkeypatch, directory, **values)
    yield run
    shutil.rmtree(directory.absolute(), ignore_errors=True)


@pytest.fixture(scope="module")
def reject_all(corpus, tmp_path_factory):  # noqa: F811
    """G10: a VAD that hears nothing, so only the VAD-free fallback can find words."""
    with pipeline_run(tmp_path_factory, "reject", corpus=corpus, probability=0.0) as run:
        yield run


@pytest.fixture(scope="module")
def reject_rejoined(corpus, tmp_path_factory):  # noqa: F811
    """Session 3: a guest who left and rejoined, again with a VAD that hears nothing."""
    with pipeline_run(tmp_path_factory, "rejoined", corpus=corpus, probability=0.0, session=REJOINED) as run:
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
    """G10 with the VAD rejecting everything: activity, floors and fallback words per track, the gate per person."""
    from cassini_talk import recognition_fallback as fallback

    rows = expected(session)
    breakdown = run.diagnostics("no-words-breakdown")
    used = run.diagnostics("few-words-fallback")
    assert breakdown, "no breakdown diagnostics"
    floor_ms = fallback.FLOOR_FRAMES * fallback.FRAME * 1000 // fallback.RATE
    for name, row in breakdown.items():
        track = by_job(session)[name]
        # Steady noise and silence are never activity; speech and bleeds may be.
        assert row["activeSeconds"] * 1000 <= total(sounding_ms(track["id"], session=session)), f"{track['id']} active"
        assert within(row["activeSeconds"], rows["tracks"][track["id"]]["activeSeconds"]), f"{track['id']} band"
        # GNOME has a per-track floor only with FLOOR_FRAMES audible frames; compare there.
        audible = total(sounding_ms(track["id"], noise=True, session=session))
        assert (row["floorDb"] is not None) == (audible >= floor_ms), f"{track['id']} has a floor"
        if row["floorDb"] is not None:
            low, high = rows["tracks"][track["id"]]["floorDb"]
            assert low <= row["floorDb"] <= high, f"{track['id']} floor {row['floorDb']} not in {low}..{high}"
    # D63 #1: every audible track of a firing person has its entry, in track order, and only those.
    decoded = [i for i in tracks(session) if run.keep is None or i in run.keep]
    firing = [job_id(i) for i in decoded if rows["fallback"][person(tracks(session)[i])]]
    assert sorted(used) == sorted(firing), f"fallback per person: {sorted(used)}"
    entries = run.summary.get("fallback", {}).get("tracks", [])
    assert [e["trackId"] for e in entries] == [by_job(session)[name]["id"] for name in firing], "provenance"
    for name, entry in zip(firing, entries, strict=True):
        row, track = used[name], by_job(session)[name]
        assert within(row["activeSeconds"], rows["tracks"][track["id"]]["activeSeconds"]), f"{track['id']} band"
        assert within(row["personActiveSeconds"], rows["personActiveSeconds"][person(track)]), f"{track['id']} pooled"
        assert entry["personActiveSeconds"] == row["personActiveSeconds"] >= fallback.MIN_ACTIVE_SECONDS
        assert row["vadWords"] == 0 and (row["used"] == (row["fallbackWords"] > 0)), track["id"]
        assert row["fallbackWords"] == len(run.words.get(name, [])), track["id"]
        if name in breakdown:
            assert row["floorDb"] == breakdown[name]["floorDb"], f"{track['id']} one floor"
    # Without VAD words, a track can only have words through the fallback.
    assert all(used.get(name, {}).get("used") for name, words in run.words.items() if words), "words without fallback"
    speaking = {person(t) for t in tracks(session).values() if run.words.get(job_id(t["id"]))}
    assert speaking <= {key for key, ms in rows["speechMs"].items() if ms}, "who speaks"


def test_vad_rejecting_everything_falls_back_per_person(reject_all, reject_rejoined):
    for run, session in ((reject_all, CALL), (reject_rejoined, REJOINED)):
        check_fallback(run, session)
        check_no_words(run, session)
        fallen = {person(by_job(session)[name]) for name, row in run.diagnostics("few-words-fallback").items()}
        assert fallen == {key for key, value in expected(session)["fallback"].items() if value}
        # D52.4: the stored provenance keeps each entry and its pooled seconds.
        stored = run.meeting.metadata["recordingDetails"]["writingDown"]["recognitionFallback"]["tracks"]
        assert stored == run.summary["fallback"]["tracks"]


def test_the_fallback_check_fails_without_breakdown_diagnostics(reject_all):
    from dataclasses import replace

    with pytest.raises(AssertionError, match="no breakdown diagnostics"):
        check_fallback(replace(reject_all, summary=dict(reject_all.summary, diagnostics=[])))


def test_vad_accepting_everything_gives_words_only_in_owned_presence(accept_all):
    check_no_words(accept_all)
    speaking = {person(by_job()[name]) for name, words in accept_all.words.items() if words}
    assert speaking == {key for key, ms in expected()["speechMs"].items() if ms}


@pytest.fixture(scope="module", params=list(SESSIONS))
def midpoints(corpus, tmp_path_factory, request):  # noqa: F811
    """D63 #3: each session's wordMidpoints words, recognised on the VAD path of their track."""
    session = request.param
    script = {}
    for row in expected(session)["wordMidpoints"]:
        script.setdefault(job_id(row["track"]), []).append(row["ms"])
    keep = {row["track"] for row in expected(session)["wordMidpoints"]}
    values = dict(corpus=corpus, model=Mute, keep=keep, session=session, script=script)
    with pipeline_run(tmp_path_factory, "midpoints", **values) as run:
        yield session, run


def test_words_are_written_only_with_their_midpoint_in_presence(midpoints):
    session, run = midpoints
    written = {word.text for word in run.meeting.words}
    for row in expected(session)["wordMidpoints"]:
        text = label(job_id(row["track"]), row["ms"])
        assert (text in written) == row["keep"], f"{row['track']} {row['ms']} keep {row['keep']}"
    assert written == {label(job_id(r["track"]), r["ms"]) for r in expected(session)["wordMidpoints"] if r["keep"]}


def test_words_outside_presence_never_count_towards_few_words(corpus, tmp_path_factory):  # noqa: F811
    """D63 #1 and #3: ten VAD words in d-return's gap are never written, so source-d still falls back."""
    rows = expected(REJOINED)
    (gap,) = rows["tracks"]["d-return"]["gaps"]
    words = [[gap[0] + 150 * n, gap[0] + 150 * n + 100] for n in range(C["FEW_WORDS"])]
    assert all(gap[0] <= w[0] and w[1] <= gap[1] for w in words)
    keep = [i for i, t in tracks(REJOINED).items() if person(t) == "source-d"]
    values = dict(corpus=corpus, keep=keep, session=REJOINED, script={job_id("d-return"): words})
    with pipeline_run(tmp_path_factory, "gap-words", **values) as run:
        used = run.diagnostics("few-words-fallback")
        assert sorted(used) == sorted(job_id(i) for i in keep), "source-d falls back"
        assert all(row["vadWords"] == 0 and row["used"] for row in used.values())
        written = {word.text for word in run.meeting.words}
        assert written and not written & {label(job_id("d-return"), w) for w in words}, "gap words written"
        check_no_words(run, REJOINED)


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
    from cassini_talk.transcript import build_turns, gap_break

    gaps = run.meeting.turn_gaps
    turns = build_turns(list(run.meeting.words), gaps=gaps)
    # D63 #2: neither grouper joins two words across a gap range of their speaker.
    for grouped in (turns, run.meeting.turns):
        for turn in grouped:
            for first, second in zip(turn.words, turn.words[1:], strict=False):
                assert not gap_break(gaps, first, second), f"{turn.speaker} turn spans a gap at {first}, {second}"
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


def test_module_runs_leave_no_fake_child_patched_behind(reject_all, reject_rejoined, accept_all, overrun):
    import os
    import sys
    import types

    from cassini_talk import diarization_audio
    from cassini_talk import speech_pipeline as pipeline

    patched = (pipeline.SpeechDetector, pipeline.resampled_chunks, pipeline.recognise_segment, diarization_audio.rejoin)
    for value in patched:
        assert "<locals>" not in value.__qualname__, f"{value.__qualname__} still patched"
    assert not isinstance(sys.modules.get("cassini_talk.nemotron"), types.SimpleNamespace), "fake diarizer"
    for run in (reject_all, reject_rejoined, accept_all, overrun):
        assert os.environ.get("XDG_CACHE_HOME") != str(run.meeting.directory.parents[1] / "cache")


# Turns at gap ranges (D63 #2) --------------------------------------------------------------------


def reading_sizes(sizes, pairs):
    """The expected turn sizes, cut again where reading_turns starts a 30 s paragraph."""
    result, at = [], 0
    for size in sizes:
        start = None
        for begin, _end in pairs[at : at + size]:
            if start is None or begin - start >= 30000:
                result.append(0)
                start = begin
            result[-1] += 1
        at += size
    return result


def markdown_sizes(meeting, words):
    from dataclasses import replace

    from cassini_talk.export_model import markdown, model_projection

    value = dict(model_projection(replace(meeting, words=tuple(words), transcript="done")), verbatim=True)
    return [len(line.split()) for line in markdown(value).split("\n") if line.startswith("w")]


def check_turn_cases(meetings):
    from cassini_talk.meeting_text import reading_turns
    from cassini_talk.portable import Word
    from cassini_talk.transcript import build_turns

    for session, meeting in meetings.items():
        rows = expected(session)
        ranges = {speaker(key): tuple((16 * a, 16 * b) for a, b in ms) for key, ms in rows["turnBreaksMs"].items()}
        assert meeting.turn_gaps == {key: value for key, value in ranges.items() if value}, "gap ranges"
        for case in rows["turnCases"]:
            key = speaker(case["speaker"])
            words = [Word(key, a, b, f"w{i}") for i, (a, b) in enumerate(case["words"])]
            assert [len(t.words) for t in build_turns(words, gaps=meeting.turn_gaps)] == case["turns"], case
            read = [len(t.words) for t in reading_turns(words, gaps=meeting.turn_gaps)]
            assert read == reading_sizes(case["turns"], case["words"]), case
            assert markdown_sizes(meeting, words) == case["turns"], case


def test_turn_cases_split_only_at_gap_ranges(corpus):  # noqa: F811
    check_turn_cases(corpus.meetings)


def test_scanned_meeting_splits_a_long_at_its_gap_in_every_grouper(corpus, tmp_path):  # noqa: F811
    from test_library import write_metadata

    from cassini_talk.export_model import markdown, model_projection
    from cassini_talk.library import LibraryScanner, turn_gaps

    (case,) = (c for c in expected()["turnCases"] if c["speaker"] == "source-a" and len(c["words"]) == 4)
    folder = copy_meeting(corpus, tmp_path / "library").directory
    items = [dict(speaker="track-source-a", startMs=a, endMs=b, text=f"w{i}") for i, (a, b) in enumerate(case["words"])]
    (folder / "transcript.json").write_text(json.dumps(dict(language="en", items=items)))
    reference = dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
    write_metadata(folder, transcript=dict(status="done", reference=reference))
    (meeting,) = LibraryScanner(tmp_path / "library").scan().meetings
    assert case["turns"] == [2, 2]
    assert [len(turn.words) for turn in meeting.turns] == case["turns"], "reading"
    assert [turn.text for turn in meeting.preview_turns] == ["w0 w1", "w2 w3"], "preview"
    body = markdown(model_projection(meeting))
    assert [line for line in body.split("\n") if line.startswith("w")] == ["w0 w1", "w2 w3"], "markdown"
    # A file read without its session folder takes the same ranges from x["codemyriad.io"].tracks.
    stored = [
        dict(
            {key: track[key] for key in ("id", "kind", "sourceTrackId", "gaps") if key in track},
            spans=[{key: track[key] for key in ("file", "sampleRate", "frames", "startOffsetMs")}],
        )
        for track in json.loads((folder / "session.json").read_text())["tracks"]
    ]
    assert turn_gaps((), {}, {"codemyriad.io": {"tracks": stored}}) == meeting.turn_gaps


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

    monkeypatch.setattr(transcription, "session_presence", lambda track: None)


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
