# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare scanner output with independently hand-authored shared Library rows."""

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from cassini_talk.library import LibraryScanner

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "ux/library-vectors"
CASES = json.loads((VECTORS / "cases.json").read_text())
OUTCOMES = json.loads((VECTORS / "outcome-lines.json").read_text())
SIDECAR = dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")


@pytest.fixture(scope="session")
def shared_library(tmp_path_factory):
    directory = tmp_path_factory.mktemp("shared-library") / "trees"
    subprocess.run([sys.executable, str(VECTORS / "make.py"), str(directory)], check=True)
    return directory


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_shared_library_vector(case, shared_library):
    directory = shared_library / case["id"]
    activity = json.loads((directory / "activity.json").read_text())
    actual = (
        LibraryScanner(directory / "root")
        .scan(capture_activity=activity)
        .as_dict(now_ms=CASES["nowMs"], tz=CASES["tz"])
    )
    expected = json.loads((VECTORS / "expected" / (case["id"] + ".json")).read_text())
    if artifact_root := os.environ.get("CASSINI_UX_ARTIFACTS"):
        output = Path(artifact_root) / "library-cases"
        output.mkdir(parents=True, exist_ok=True)
        (output / (case["id"] + ".json")).write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
    assert actual == expected


def scan(root, activity=None):
    return LibraryScanner(root).scan(capture_activity=activity or {})


def outcome(root):
    """Shared test 8: the real Details lines, as GNOME's Details dialog shows them."""
    from cassini_talk.capture_details import read_details

    (meeting,) = scan(root).meetings
    lines = [(line.id, line.text, line.warning, line.gap_lines) for line in read_details(meeting).audio]
    words = dict(Counter(word.speaker for word in meeting.words))
    return lines, meeting.no_speech, words


def expected_outcome(case):
    """Render the hand-written catalog ids and arguments with GNOME's own catalog and formats."""
    from cassini_talk.strings import ENTRIES, text
    from cassini_talk.ux_formats import meeting_duration

    value = OUTCOMES["cases"][case]
    lines = []
    for line in value["audio"]:
        assert set(line["args"]) == set(ENTRIES[line["id"]].get("placeholders", {})), line
        source = line["args"]["source"]
        args = dict(source=text(source["catalog"]) if "catalog" in source else source["text"])
        if "duration" in line["args"]:
            args["duration"] = meeting_duration(line["args"]["duration"]["ms"])
        lines.append((line["id"], text(line["id"], **args), line["problem"], ()))
    return lines, value["noSpeech"], value["visibleWords"]


def dump(name, value):
    if artifact_root := os.environ.get("CASSINI_UX_ARTIFACTS"):
        output = Path(artifact_root) / "outcome-lines"
        output.mkdir(parents=True, exist_ok=True)
        lines, no_speech, words = value
        rows = [dict(id=i, text=t, problem=w, gapLines=list(g)) for i, t, w, g in lines]
        document = dict(audio=rows, noSpeech=no_speech, visibleWords=words)
        (output / (name + ".json")).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def test_outcome_lines_cover_exactly_the_shared_test_8_cases():
    assert list(OUTCOMES["cases"]) == [case["id"] for case in CASES["cases"] if "T8" in case["rules"]]


@pytest.mark.parametrize("case", list(OUTCOMES["cases"]))
def test_outcome_lines(case, shared_library):
    """D67: control, others silent, never captured and failed read as three distinct states; guards pin R13,
    D67.2 and D67.8."""
    actual = outcome(shared_library / case / "root")
    dump(case, actual)
    assert actual == expected_outcome(case)


def edit_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def never_captured_was_captured(folder):
    def change(value):
        for row in [*value["captureOutcome"]["sources"], *value["tracks"]]:
            row["captureOutcome"] = "captured"
        value["state"] = "completed"

    edit_json(folder / "session.json", change)


def failed_became_done(folder):
    edit_json(
        folder / "meeting.json",
        lambda value: value.update(
            transcript=dict(value["transcript"], status="done", reference=SIDECAR),
            error=None,
        ),
    )


def failed_lost_its_error(folder):
    edit_json(folder / "meeting.json", lambda value: value.update(error=None))


def silent_tracks_dropped(folder):
    edit_json(folder / "meeting.json", lambda value: value["recordingDetails"]["writingDown"].pop("silentTracks"))


def return_track_silent_too(folder):
    edit_json(
        folder / "meeting.json",
        lambda value: value["recordingDetails"]["writingDown"]["silentTracks"].append("guest-return"),
    )


def retry_became_done(folder):
    edit_json(
        folder / "meeting.json",
        lambda value: value.update(transcript=dict(value["transcript"], status="done", reference=SIDECAR), error=None),
    )


def reference_dropped(folder):
    edit_json(folder / "meeting.json", lambda value: value["transcript"].update(reference=None))


def low_audio_was_captured(folder):
    def change(value):
        for row in [*value["captureOutcome"]["sources"], *value["tracks"]]:
            row["captureOutcome"] = "captured"
        value["state"] = "completed"
        del value["problem"], value["problemKind"]

    edit_json(folder / "session.json", change)


@pytest.mark.parametrize(
    ("case", "mutate", "caught"),
    [
        ("t8-others-never-captured", never_captured_was_captured, True),
        ("t8-others-transcription-failed", failed_became_done, True),
        ("t8-others-transcription-failed", failed_lost_its_error, False),
        ("t8-others-silent", silent_tracks_dropped, True),
        ("t8-silent-per-source", return_track_silent_too, True),
        ("t8-retry-failed-stale-silent", retry_became_done, True),
        ("t8-failed-referenced-words", reference_dropped, True),
        ("t8-none-referenced-words", reference_dropped, True),
        ("t8-low-audio-silent", low_audio_was_captured, True),
    ],
    ids=lambda value: getattr(value, "__name__", value),
)
def test_outcome_file_mutants(case, mutate, caught, shared_library, tmp_path):
    """Data mutants: each changed file must fail the shared expectations, except one R3 absorbs.

    A missing stored error on a failed meeting is supplied by R3, so that file reads the same.
    """
    root = tmp_path / "root"
    shutil.copytree(shared_library / case / "root", root, symlinks=True)
    (folder,) = root.iterdir()
    mutate(folder)
    expected_row = json.loads((VECTORS / "expected" / (case + ".json")).read_text())
    row = scan(root).as_dict(now_ms=CASES["nowMs"], tz=CASES["tz"])
    same = row == expected_row and outcome(root) == expected_outcome(case)
    assert same is not caught
