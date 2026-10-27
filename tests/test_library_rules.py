# SPDX-License-Identifier: GPL-3.0-or-later
"""Common D6 rows and regression guards for blocking/caching bugs in the review."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_library import capture, received, write_metadata

from cassini_viewer import library
from cassini_viewer.library_schema import SCHEMA, valid

ROOT = Path(__file__).resolve().parents[1]
REF = dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")


def test_dangling_root_fails_while_absent_is_empty_and_linked_root_works(tmp_path):
    missing = tmp_path / "absent"
    assert not library.scan_library(missing).unavailable
    root = tmp_path / "link"
    root.symlink_to(missing, target_is_directory=True)
    assert library.scan_library(root).unavailable
    session = capture(missing)
    assert library.scan_library(root).meetings[0].id == session.manifest["id"]


@pytest.mark.parametrize("name", ["session.json", "meeting.json", "transcript.json", "track-microphone.wav"])
def test_fifo_is_never_opened_as_a_stream(tmp_path, name):
    session = capture(tmp_path)
    target = session.directory / name
    target.unlink(missing_ok=True)
    os.mkfifo(target)
    # A subprocess deadline makes a regression fail rather than hanging pytest.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from cassini_viewer.library import scan_library;import sys;assert len(scan_library(sys.argv[1]).meetings)==1",
            str(tmp_path),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        timeout=3,
        check=True,
    )


@pytest.mark.parametrize("status", ["queued", "running", "failed", "done", "damaged", "none"])
def test_missing_body_status_precedence_and_actionable_error(tmp_path, status):
    session = capture(tmp_path)
    error = dict(id="transcribe-failed", action="Try again")
    write_metadata(
        session.directory,
        transcript=dict(status=status, reference=REF),
        etaMinutes=4 if status in ("queued", "running") else None,
        error=error,
    )
    meeting = library.scan_library(tmp_path).meetings[0]
    assert meeting.transcript == ("damaged" if status == "done" else status)
    assert meeting.error.id == error["id"] and meeting.error.action == error["action"]
    assert meeting.badge().kind == ("transcribing" if status in ("queued", "running") else "needsAttention")
    assert meeting.can_write_down == (status in ("none", "failed"))


def test_quote_override_normalises_one_outer_pair_and_is_gated_by_words(tmp_path):
    session = capture(tmp_path)
    body = dict(items=[dict(speaker="a", startMs=0, endMs=100, text="Body")])
    (session.directory / "transcript.json").write_text(json.dumps(body))
    write_metadata(
        session.directory,
        transcript=dict(status="done", reference=REF),
        quoteOverride=dict(atMs=0, text="  “Already\t quoted”  "),
    )
    assert library.scan_library(tmp_path).meetings[0].quote_line == "“Already quoted”"
    write_metadata(
        session.directory, transcript=dict(status="none", reference=REF), quoteOverride=dict(atMs=0, text='"No quote"')
    )
    assert library.scan_library(tmp_path).meetings[0].quote_line is None


def test_incremental_row_and_parsed_sidecar_cache_survive_metadata_changes(tmp_path, monkeypatch):
    sessions = [capture(tmp_path) for _ in range(4)]
    body = dict(items=[dict(speaker="a", startMs=i * 100, endMs=i * 100 + 50, text="Word") for i in range(1000)])
    for session in sessions:
        write_metadata(session.directory, transcript=dict(status="done", reference=REF))
        (session.directory / "transcript.json").write_text(json.dumps(body))
    counts = dict(json=0, words=0)
    original_json, original_words = library._json, library._sidecar_words

    def read_json(*args):
        counts["json"] += 1
        return original_json(*args)

    def read_words(*args):
        counts["words"] += 1
        return original_words(*args)

    monkeypatch.setattr(library, "_json", read_json)
    monkeypatch.setattr(library, "_sidecar_words", read_words)
    scanner = library.LibraryScanner(tmp_path)
    first = scanner.scan()
    assert counts["words"] == 4
    counts.update(json=0, words=0)
    assert all(a is b for a, b in zip(first.meetings, scanner.scan().meetings, strict=True))
    assert counts == dict(json=0, words=0)
    target = sessions[0]
    write_metadata(target.directory, title="Renamed", titleSource="user", transcript=dict(status="done", reference=REF))
    second = scanner.scan(changed_directories={target.directory})
    assert counts == dict(json=1, words=0)
    for row in second.meetings:
        previous = next(m for m in first.meetings if m.id == row.id)
        assert (row is previous) == (row.id != target.manifest["id"])
    assert next(m for m in second.meetings if m.id == target.manifest["id"]).title == "Renamed"


def test_unchanged_received_audio_and_embedded_words_are_not_reparsed(tmp_path, monkeypatch):
    directory = received(tmp_path)
    scanner = library.LibraryScanner(tmp_path)
    first = scanner.scan().meetings[0]

    def forbidden(*_args):
        raise AssertionError("Unchanged Opus was parsed or hashed again")

    monkeypatch.setattr(library.portable, "compute_audio_digest", forbidden)
    monkeypatch.setattr(library.portable, "read_header", forbidden)
    monkeypatch.setattr(library.portable, "load_words", forbidden)
    value = json.loads((directory / "meeting.json").read_text())
    value["title"], value["titleSource"] = "Renamed", "user"
    (directory / "meeting.json").write_text(json.dumps(value))
    second = scanner.scan(changed_directories={directory}).meetings[0]
    assert second.title == "Renamed" and second.quote_line == first.quote_line
    assert second.duration_ms == 60 and second.transcript == "done"


def test_metadata_validator_agrees_with_the_actual_schema_for_common_invalid_documents(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    session = capture(tmp_path)
    baseline = write_metadata(session.directory)
    validator = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())
    variants = [baseline]
    for field, values in {
        "revision": [-1, True, "0"],
        "notes": ["bad", [dict(atMs=-1, text="x")]],
        "title": ["", 4],
        "transcript": [dict(status="done", reference="bad"), dict(status="done", reference=[])],
        "quoteOverride": [dict(atMs=0, text="")],
        "speakers": [[dict(id="a", name=None, provenance="user", colourIndex=0)]],
    }.items():
        for value in values:
            variant = copy.deepcopy(baseline)
            variant[field] = value
            variants.append(variant)
    for variant in variants:
        assert valid(variant) == validator.is_valid(variant)


@pytest.mark.parametrize("offset,frames,rate,end", [(12.75, 48, 48000, 13), (0.98, 1, 48000, 1), (0.875, 1, 8000, 1)])
def test_fractional_display_end_is_exact_floor_of_the_complete_sum(offset, frames, rate, end):
    track = library.CaptureTrack(Path("unused.wav"), frames, rate, offset, True)
    assert track.display_end_ms == end and track.start_offset_ms == offset


def test_noninteger_eta_invalidates_whole_metadata_but_null_keeps_job(tmp_path):
    session = capture(tmp_path)
    write_metadata(
        session.directory,
        title="Invalid title",
        titleSource="user",
        transcript=dict(status="running", reference=None),
        etaMinutes="later",
    )
    invalid = library.scan_library(tmp_path).meetings[0]
    assert invalid.title == "Capture title" and invalid.transcript == "none"
    write_metadata(session.directory, transcript=dict(status="running", reference=None), etaMinutes=None)
    assert library.scan_library(tmp_path).meetings[0].badge().text == "Transcribing…"


def test_same_uuid_under_both_prefixes_keeps_two_rows_with_distinct_internal_keys(tmp_path):
    session = capture(tmp_path)
    session.manifest["createdAt"] = "2026-09-01T00:00:00Z"
    (session.directory / "session.json").write_text(json.dumps(session.manifest))
    directory = received(tmp_path, "002-plain-audio.opus")
    destination = tmp_path / ("Received-" + session.manifest["id"])
    directory.rename(destination)
    metadata = json.loads((destination / "meeting.json").read_text())
    metadata["id"] = session.manifest["id"]
    metadata["received"]["importedAtMs"] = 1788220800000
    (destination / "meeting.json").write_text(json.dumps(metadata))
    rows = library.scan_library(tmp_path).meetings
    assert len(rows) == 2 and rows[0].id == rows[1].id == session.manifest["id"]
    assert [r.key for r in rows] == sorted([session.directory.name, destination.name])


def test_unknown_duration_omits_duration_without_inventing_copy():
    from cassini_viewer import ux_formats as fmt

    assert fmt.row_meta(None, []) is None
    assert fmt.row_meta(None, ["Marta"]) == "Marta"
    context = dict(now_ms=1793107080000, tz="Europe/Rome")
    assert fmt.preview_subtitle(1793088000000, None, **context) == "Today, 09:00"
    assert fmt.preview_subtitle(None, None, **context) == "Date unknown"
