# SPDX-License-Identifier: GPL-3.0-or-later
"""D52 source facts distinguish partial, missing, failed and quiet capture."""

import shutil
from copy import deepcopy
from fractions import Fraction

import pytest

from cassini_talk.source_report import sources


def session():
    rows = [
        dict(sourceTrackId=name, kind="remote", label="Guest", captureOutcome=outcome, audioMs=duration, trackIds=ids)
        for name, outcome, duration, ids in (
            ("quiet", "captured", 2000, ["span-1", "span-2"]),
            ("never", "neverAudio", 0, []),
            ("short", "lowAudio", 500, ["short"]),
            ("failed", "failed", 0, []),
        )
    ]
    return dict(
        captureOutcome=dict(sources=rows),
        tracks=[
            dict(id="span-1", frames=48000, sampleRate=48000, gapFrames=480, droppedFrames=480),
            dict(id="span-2", frames=44100, sampleRate=44100, gapFrames=1, droppedFrames=0),
            dict(id="short", frames=24000, sampleRate=48000),
        ],
    )


def test_zero_audio_missing_short_and_failed_keep_distinct_facts_without_double_counting_gaps():
    value = session()
    before = deepcopy(value)
    rows = {row.identity: row for row in sources(value, {}, live=False)}
    assert list(rows) == ["quiet", "never", "short", "failed"]
    assert [row.outcome for row in rows.values()] == ["captured", "neverAudio", "lowAudio", "failed"]
    assert not rows["quiet"].problem and all(rows[key].problem for key in ("never", "short", "failed"))
    assert rows["quiet"].padding_ms == Fraction(10) + Fraction(10, 441)
    assert rows["quiet"].dropped_ms == 10  # May describe the same first 10 ms; never sum them.
    assert value == before


def test_live_startup_is_not_a_missing_audio_warning_but_real_failure_is():
    rows = {row.identity: row for row in sources(session(), {}, live=True)}
    assert not rows["never"].problem and not rows["short"].problem
    assert rows["failed"].problem and all(not row.final for row in rows.values())
    assert sources({}, {}, live=False) == ()


def test_never_selected_sources_are_neutral_without_erasing_the_reported_outcome():
    value = session()
    for row in value["captureOutcome"]["sources"]:
        row["captureExpected"] = False
    rows = sources(value, {}, live=False)
    assert [row.outcome for row in rows] == ["captured", "neverAudio", "lowAudio", "failed"]
    assert all(not row.expected and not row.problem for row in rows)


@pytest.mark.parametrize("flag", [None, 0, "false"])
def test_invalid_selection_flag_is_not_coerced_to_hide_a_failure(flag):
    value = session()
    value["captureOutcome"]["sources"][3]["captureExpected"] = flag
    assert sources(value, {}, live=False) == ()


@pytest.mark.parametrize("change", ["missing-track", "duplicate-track", "bad-count", "bad-rate"])
def test_unknown_or_conflicting_track_diagnostics_are_not_claimed_as_zero(change):
    value = session()
    if change == "missing-track":
        value["tracks"].pop(0)
    elif change == "duplicate-track":
        value["tracks"].append(dict(value["tracks"][0]))
    else:
        value["tracks"][0]["gapFrames" if change == "bad-count" else "sampleRate"] = -1
    row = sources(value, {}, live=False)[0]
    assert row.padding_ms is None
    assert row.audio_ms == 2000 and row.outcome == "captured"


def test_portable_record_has_the_same_facts_after_cache_deletion(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Real publication requires GStreamer")
    from test_capture_report import captured

    from cassini_talk.capture_details import read_details
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_publication import publish_stopped

    root = tmp_path / "Library"
    capture = captured(root)
    published, directory = publish_stopped(capture), capture.directory
    meeting = LibraryScanner(root).scan().meetings[0]
    before = read_details(meeting).sources
    assert {row.identity: row.outcome for row in before} == {"missing": "neverAudio", "zeros": "captured"}
    shutil.rmtree(directory)
    after = LibraryScanner(root).scan().meetings[0]
    assert after.published_file == root / published.name
    assert read_details(after).sources == before


@pytest.mark.parametrize("tracks", [None, "not tracks", [None, True, {"id": []}]])
def test_malformed_diagnostic_tracks_do_not_hide_valid_source_outcomes(tracks):
    value = session()
    value["tracks"] = tracks
    rows = sources(value, {}, live=False)
    assert len(rows) == 4 and rows[0].outcome == "captured" and rows[0].padding_ms is None


def test_unselected_source_stays_neutral_through_actual_publication_and_cache_deletion(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Real publication requires GStreamer")
    from test_capture_record import declare, pcm

    from cassini_talk.capture_details import read_details
    from cassini_talk.capture_sources import BrowserStreams
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_publication import capture_parent, publish_stopped
    from cassini_talk.recording_session import BrowserHost

    root = tmp_path / "Library"
    host = BrowserHost(capture_parent(root))
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="choice"))
    host.audio_sink = BrowserStreams(host.session, me=False, others=False)
    try:
        host.session.add_track("native-mic", "Me", "microphone", 48000, 1, label_source="device")
        host.session.append("native-mic", b"\x01\x00" * 48000, 0)
        assert host.handle(declare("unused", "unused")) == dict(type="ack")
        assert host.handle(pcm("unused", 48000)) == dict(type="ack")
        host.handle(dict(type="stop", captureId="choice"))
        assert host.session.manifest["state"] == "completed"
        publish_stopped(host.session)
        report = read_details(LibraryScanner(root).scan().meetings[0])
        (row,) = report.sources
        assert row.identity == "unused" and row.outcome == "neverAudio"
        assert not row.expected and not row.problem
        shutil.rmtree(host.session.directory)
        after = read_details(LibraryScanner(root).scan().meetings[0])
        assert after.sources == report.sources
    finally:
        host.close()


def test_approved_source_copy_keeps_outcomes_and_unequal_loss_counters_separate():
    rows = sources(session(), {}, live=False)
    assert [row.line for row in rows] == [
        "Guest: 2 s recorded.",
        "Guest: no audio was received.",
        "Guest: less than a second of audio was received.",
        "Guest: audio couldn’t be recorded.",
    ]
    assert rows[0].gap_lines == ("Guest: 1 s of audio is missing.", "Dropped audio: 1 s")
    live = sources(session(), {}, live=True)
    assert live[1].line == "Guest"
    assert live[2].line == "Guest: 1 s recorded."
    value = session()
    value["captureOutcome"]["sources"][3]["captureExpected"] = False
    unused = sources(value, {}, live=False)[3]
    assert unused.line == "Guest: not selected for recording." and not unused.gap_lines


def test_d52_catalog_entries_are_the_exact_approved_six_language_objects():
    import hashlib
    import json
    from pathlib import Path

    from cassini_talk import strings

    shared = Path(__file__).parent / "ux" / "d52" / "strings.json"
    assert (
        hashlib.sha256(shared.read_bytes()).hexdigest()
        == "6284099c78168ed210983748f628d60ac800ad4ea0bb58b17d2f7bd2d75ff738"
    )
    catalog = json.loads(Path(strings.__file__).with_name("strings.json").read_bytes())
    actual = {entry["id"]: entry for entry in catalog["entries"]}
    approved = json.loads(shared.read_bytes())
    assert len(approved) == 11
    for entry in approved:
        assert actual[entry["id"]] == entry
        assert set(entry["l10n"]) == {"it", "fr", "es", "pt-BR", "pt-PT"}
        assert strings.text(entry["id"], source="Guest", duration="1 min") == entry["en"].format(
            source="Guest", duration="1 min"
        )


def test_source_rows_and_copy_text_survive_publication_and_cache_deletion(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Real publication requires GStreamer")
    from test_capture_report import captured

    from cassini_talk.capture_details import read_details
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_publication import publish_stopped

    root = tmp_path / "Library"
    capture = captured(root)
    publish_stopped(capture)
    before = read_details(LibraryScanner(root).scan().meetings[0])
    assert "Audio sources\n" in before.plain_text()
    assert all(source.line in before.plain_text() for source in before.sources)
    shutil.rmtree(capture.directory)
    after = read_details(LibraryScanner(root).scan().meetings[0])
    assert [s.line for s in after.sources] == [s.line for s in before.sources]
    assert [s.gap_lines for s in after.sources] == [s.gap_lines for s in before.sources]


@pytest.mark.parametrize("selected", [False, True])
def test_real_host_source_failure_without_a_wav_reaches_live_worker_and_details(tmp_path, selected):
    from cassini_talk.capture_details import read_details
    from cassini_talk.capture_sources import BrowserStreams
    from cassini_talk.library import LibraryScanner
    from cassini_talk.recording_details import recording_observation
    from cassini_talk.recording_session import BrowserHost

    host = BrowserHost(tmp_path)
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="live-report"))
    host.audio_sink = BrowserStreams(host.session, others=selected)
    try:
        host.session.add_track("mic", "Me", "microphone", 48000, 1)
        host.session.append("mic", bytes(9600), 0)
        assert host.handle(
            dict(
                type="sources",
                discoveredSources=[dict(sourceTrackId="missing-person", kind="remote", label="Guest", failed=True)],
            )
        ) == dict(type="ack")
        meeting = LibraryScanner(tmp_path).scan(capture_activity={host.session.manifest["id"]: "recording"}).meetings[0]
        assert recording_observation(meeting)["source_problem"] == selected
        report = read_details(meeting)
        assert len(report.sources) == 1 and report.sources[0].problem == selected
        assert report.sources[0].line in report.plain_text()
        assert len(host.session.manifest["tracks"]) == 1, "Failure without a WAV must remain visible"
    finally:
        host.close()


@pytest.mark.parametrize("kind", ["microphone", "system"])
def test_isolated_native_writer_failure_warns_while_healthy_browser_audio_continues(tmp_path, monkeypatch, kind):
    import errno

    from test_capture_record import declare, pcm

    from cassini_talk.library import LibraryScanner
    from cassini_talk.recording_details import recording_observation
    from cassini_talk.recording_session import BrowserHost

    host = BrowserHost(tmp_path)
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="native-failure"))
    session = host.session
    try:
        session.add_track("native", "Native input", kind, 48000, 1, label_source="device")
        session.append_native("native", b"\x01\x00" * 48000, 0)
        writer = session.writers["native"]

        def broken():
            if not writer.file.closed:
                raise OSError(errno.ENOSPC, "Generated native WAV sync failure")

        monkeypatch.setattr(writer, "sync", broken)
        session.checkpoint()
        assert host.handle(declare("healthy")) == dict(type="ack")
        assert host.handle(pcm("healthy", 48000)) == dict(type="ack")
        meeting = LibraryScanner(tmp_path).scan(capture_activity={session.manifest["id"]: "recording"}).meetings[0]
        assert recording_observation(meeting)["source_problem"]
        assert not session.finished
    finally:
        host.close()


# D58.4 (Mac QA L1): Details durations are never rounded down to whole minutes.
# The rule is Mac's detailsDuration (cassini-mac #150): talkTime of the floored
# seconds, with a positive amount under one second shown as 1 s, never 0 s.
DETAILS_DURATIONS = (
    (0, "0 s"),
    (Fraction(1000, 48000), "1 s"),  # one padding frame at 48 kHz
    (400, "1 s"),
    (999, "1 s"),
    (999.9, "1 s"),
    (1000, "1 s"),
    (1999, "1 s"),
    (43000, "43 s"),
    (59400, "59 s"),
    (59600, "59 s"),
    (59999, "59 s"),
    (60000, "1 min 0 s"),
    (61000, "1 min 1 s"),
    (87000, "1 min 27 s"),
    (3599999, "59 min 59 s"),
    (3600000, "1 h 00 min"),
    (3725000, "1 h 02 min"),
)


@pytest.mark.parametrize("ms, shown", DETAILS_DURATIONS, ids=lambda value: str(value))
def test_details_durations_are_whole_seconds_under_a_minute_and_never_zero_when_positive(ms, shown):
    from cassini_talk.i18n import using_language
    from cassini_talk.ux_formats import details_duration

    with using_language("en"):
        assert details_duration(float(ms) if isinstance(ms, Fraction) else ms) == shown


@pytest.mark.parametrize("ms", (float("nan"), float("inf"), -1, None, "1000"))
def test_details_duration_refuses_what_talk_time_refuses(ms):
    from cassini_talk.ux_formats import details_duration

    with pytest.raises(ValueError):
        details_duration(ms)


def one_source(outcome, ms, tracks=None, **row):
    rows = [
        dict(
            sourceTrackId="s",
            kind="remote",
            label="Guest",
            captureOutcome=outcome,
            audioMs=ms,
            trackIds=["t"] if tracks else [],
            **row,
        )
    ]
    return dict(captureOutcome=dict(sources=rows), tracks=tracks or [])


@pytest.mark.parametrize("ms", (1, 400, 500, 999, 999.9))
def test_a_saved_low_audio_source_under_a_second_says_so_in_words(ms):
    from cassini_talk.capture_details import audio_lines
    from cassini_talk.i18n import using_language

    with using_language("en"):
        (row,) = sources(one_source("lowAudio", ms), {}, live=False)
        assert row.line == "Guest: less than a second of audio was received." and row.problem
        (line,) = audio_lines((row,), [])
        assert (line.id, line.text, line.warning) == (
            "details.sources.lowAudio.underSecond",
            "Guest: less than a second of audio was received.",
            True,
        )
        # Live, low audio still reads as captured, with its (never zero) seconds.
        (live,) = sources(one_source("lowAudio", ms), {}, live=True)
        assert live.line == "Guest: 1 s recorded." and not live.problem
        # The line id names the line shown, as on Mac: captured, not lowAudio.
        assert audio_lines((live,), [])[0].id == "details.sources.captured"


@pytest.mark.parametrize(
    "outcome, ms, shown",
    (
        ("lowAudio", 1000, "Guest: only 1 s of audio was received."),  # another writer's stored row
        ("lowAudio", 1500, "Guest: only 1 s of audio was received."),
        ("captured", 0, "Guest: 0 s recorded."),
        ("captured", 400, "Guest: 1 s recorded."),
        ("captured", 1000, "Guest: 1 s recorded."),
        ("captured", 59400, "Guest: 59 s recorded."),
        ("captured", 59600, "Guest: 59 s recorded."),
        ("captured", 60000, "Guest: 1 min 0 s recorded."),
        ("captured", 61000, "Guest: 1 min 1 s recorded."),
        ("captured", 3725000, "Guest: 1 h 02 min recorded."),
    ),
)
def test_saved_source_lines_show_details_durations(outcome, ms, shown):
    from cassini_talk.capture_details import audio_lines
    from cassini_talk.i18n import using_language

    with using_language("en"):
        (row,) = sources(one_source(outcome, ms), {}, live=False)
        assert row.line == shown
        assert audio_lines((row,), [])[0].id == "details.sources." + outcome


def test_a_low_audio_source_that_was_not_selected_keeps_the_neutral_line():
    (row,) = sources(one_source("lowAudio", 500, captureExpected=False), {}, live=False)
    assert row.line == "Guest: not selected for recording." and not row.under_second and not row.problem
    assert row.line_id == "details.sources.notSelected"


@pytest.mark.parametrize(
    "outcome, final, line_id, shown",
    (
        ("lowAudio", False, "details.sources.captured", "Guest: 2 s recorded."),
        ("lowAudio", True, "details.sources.lowAudio", "Guest: only 2 s of audio was received."),
        ("neverAudio", False, "", "Guest"),
        ("neverAudio", True, "details.sources.neverAudio", "Guest: no audio was received."),
        ("captured", False, "details.sources.captured", "Guest: 2 s recorded."),
    ),
)
def test_the_line_id_names_the_line_shown(outcome, final, line_id, shown):
    from cassini_talk.i18n import using_language

    with using_language("en"):
        (row,) = sources(one_source(outcome, 2000), {}, live=not final)
        assert (row.line_id, row.line) == (line_id, shown)


@pytest.mark.parametrize(
    "gap, dropped, shown",
    (
        (1, 0, ("Guest: 1 s of audio is missing.",)),
        (14400, 0, ("Guest: 1 s of audio is missing.",)),  # 300 ms
        (0, 14400, ("Dropped audio: 1 s",)),
        (48000 * 59 + 28800, 0, ("Guest: 59 s of audio is missing.",)),  # 59.6 s
        (48000 * 60, 48000 * 61, ("Guest: 1 min 0 s of audio is missing.", "Dropped audio: 1 min 1 s")),
    ),
)
def test_gap_and_dropped_lines_are_never_zero_or_whole_minutes(gap, dropped, shown):
    from cassini_talk.i18n import using_language

    track = dict(id="t", frames=48000 * 120, sampleRate=48000, gapFrames=gap, droppedFrames=dropped)
    with using_language("en"):
        (row,) = sources(one_source("captured", 120000, [track]), {}, live=False)
        assert row.gap_lines == shown


@pytest.mark.parametrize("lang", ("en", "it", "fr", "es", "pt-BR", "pt-PT"))
def test_under_second_line_is_the_shared_catalog_text_in_every_language(lang):
    import json
    from pathlib import Path

    from cassini_talk import strings
    from cassini_talk.i18n import using_language

    catalog = json.loads(Path(strings.__file__).with_name("strings.json").read_bytes())
    (entry,) = [e for e in catalog["entries"] if e["id"] == "details.sources.lowAudio.underSecond"]
    assert "gnome" in entry["platforms"] and set(entry["l10n"]) == {"it", "fr", "es", "pt-BR", "pt-PT"}
    template = entry["en"] if lang == "en" else entry["l10n"][lang]["text"]
    with using_language(lang):
        (row,) = sources(one_source("lowAudio", 480), {}, live=False)
        assert row.line == template.format(source="Guest")
        assert not any(ch.isdigit() for ch in row.line)
