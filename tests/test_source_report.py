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
