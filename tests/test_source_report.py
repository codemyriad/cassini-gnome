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
        "Guest: under 1 min recorded.",
        "Guest: no audio was received.",
        "Guest: only under 1 min of audio was received.",
        "Guest: audio couldn’t be recorded.",
    ]
    assert rows[0].gap_lines == ("Guest: under 1 min of audio is missing.", "Dropped audio: under 1 min")
    live = sources(session(), {}, live=True)
    assert live[1].line == "Guest"
    assert live[2].line == "Guest: under 1 min recorded."
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
