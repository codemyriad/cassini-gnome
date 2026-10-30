# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
from dataclasses import replace

import pytest
from details_fixture import build_library, write_json

from cassini_viewer.capture_details import UNKNOWN, read_details, timeline
from cassini_viewer.library import LibraryScanner


@pytest.fixture
def meetings(tmp_path):
    paths = build_library(tmp_path)
    scanned = {m.directory: m for m in LibraryScanner(tmp_path).scan().meetings}
    assert len(scanned) == 3
    return {key: scanned[path] for key, path in paths.items()}


def test_fallback_streams_are_audio_not_people_and_do_not_invent_page_reasons(meetings):
    meeting = meetings["fallback"]
    report = read_details(meeting)
    others = [s for s in report.spans if s.side == "The others"]
    assert [(s.source, s.start_ms, s.end_ms) for s in others] == [
        ("Browser audio", 0, 1870000),
        ("Computer sound", 1870000, 2825000),
    ]
    assert others[0].method == "3 audio streams"
    assert others[1].reason == "The browser audio connection was lost"
    assert report.ended == UNKNOWN
    copied = report.plain_text()
    assert "participants" not in copied and "reloaded" not in copied and "tab was closed" not in copied
    assert "4,000 frames · 0.5 s" in copied
    assert dict(report.writing)["Time taken"] == UNKNOWN


def test_reader_only_end_record_and_large_dc_offset_are_factual_and_do_not_write(meetings):
    meeting = meetings["microphone"]
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in meeting.directory.iterdir()}
    report = read_details(meeting)
    assert report.ended == "Stopped by you · 00:01:23"
    assert dict(report.health)["Microphone offset"] == (
        "The microphone adds a steady offset (-2060). Cassini corrects for it when finding speech; "
        "the saved audio is unchanged."
    )
    assert any(s.source == "Not recorded" and s.side == "The others" for s in report.spans)
    assert before == {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in meeting.directory.iterdir()}
    # Source evidence is not a liveness claim.
    assert read_details(replace(meeting, lifecycle="recording")).ended == "Still recording"


def test_no_events_and_unknown_offsets_never_become_inferred_history(meetings):
    meeting = meetings["no-events"]
    report = read_details(meeting)
    assert report.ended == UNKNOWN and report.notes
    assert all(not s.reason for s in report.spans)
    unknown = replace(meeting.tracks[0], start_offset_ms=None)
    report = read_details(replace(meeting, tracks=(unknown,)))
    assert len(report.spans) == 1 and report.spans[0].time == "Timing not recorded"


def test_selection_off_on_preserves_each_span_and_does_not_count_one_source_twice(meetings):
    meeting = meetings["fallback"]
    session = json.loads((meeting.directory / "session.json").read_text())
    first = replace(meeting.tracks[1], frames=80000, start_offset_ms=0, source_track_id="same")
    resumed = replace(first, id="second", start_offset_ms=20000)
    session["events"] = [
        dict(type="selection", atMs=10000, me=True, others=False, reason="user"),
        dict(type="selection", atMs=20000, me=True, others=True, reason="user"),
    ]
    rows = [
        s
        for s in timeline(replace(meeting, tracks=(first, resumed), duration_ms=30000), session)
        if s.side == "The others"
    ]
    assert [(s.source, s.start_ms, s.end_ms) for s in rows] == [
        ("Browser audio", 0, 10000),
        ("Not recorded", 10000, 20000),
        ("Browser audio", 20000, 30000),
    ]
    assert rows[0].method == rows[2].method == "1 audio stream"


def test_reports_are_whitelisted_and_do_not_read_private_voice_stores(meetings):
    meeting = meetings["microphone"]
    path = meeting.directory / "session.json"
    raw = json.loads(path.read_text())
    raw["voiceprint"] = "PRIVATE_PRINT_SENTINEL"
    raw["events"].append(dict(type="end", atMs=1, reason=["bad"]))
    raw["tracks"][0]["embedding"] = "PRIVATE_PRINT_SENTINEL"
    write_json(path, raw)
    write_json(
        meeting.directory / "speaker-turns.json",
        dict(
            format="cassini.speaker-turns.v1",
            model="nemotron-3-diarization-int8@pin",
            groups=[
                dict(
                    id="group",
                    durationMs=4000,
                    inputSHA256="0" * 64,
                    turns=[
                        dict(speaker=0, startMs=0, endMs=1000),
                        dict(speaker=0, startMs=1000, endMs=2000),
                        dict(speaker=1, startMs=2000, endMs=3000),
                    ],
                )
            ],
            embeddings="PRIVATE_PRINT_SENTINEL",
        ),
    )
    meeting = replace(meeting, speakers=(replace(meeting.speakers[0], name="Alex", provenance="recognised"),))
    report = read_details(meeting)
    assert "PRIVATE_PRINT_SENTINEL" not in report.plain_text()
    assert dict(report.writing)["Speaker separation"] == "Tell speakers apart was not installed"
    # New explicit facts are authoritative; an old cached sidecar alone does
    # not establish whether this writing attempt ran or reused separation.
    from cassini_viewer.nemotron_model import MODEL_ID, REVISION

    details = dict(
        writingDown=dict(
            wallDurationMs=4217,
            resumed=True,
            speechSegments=2,
            speakerSeparation=dict(
                status="completed",
                modelId=MODEL_ID,
                modelRevision=REVISION,
                speakersFound=2,
                reused=True,
                embedding="PRIVATE_PRINT_SENTINEL",
            ),
        )
    )
    report = read_details(replace(meeting, metadata=dict(meeting.metadata, recordingDetails=details)))
    assert dict(report.writing)["Time taken"] == "4.217 s (this run, after resuming)"
    assert dict(report.writing)["Speech segments"] == "2"
    assert dict(report.writing)["Speaker separation"].startswith("Nemotron · 2 voices")
    assert MODEL_ID + "@" + REVISION in report.plain_text()
    assert "PRIVATE_PRINT_SENTINEL" not in report.plain_text()
    assert dict(report.writing)["Saved voices recognised"] == "Alex"


def test_symlink_fifo_and_oversized_metadata_are_bounded_and_leave_files_alone(meetings, tmp_path):
    meeting = meetings["microphone"]
    session = meeting.directory / "session.json"
    session.unlink()
    os.mkfifo(session)
    transcript = meeting.directory / "transcript.json"
    transcript.write_bytes(b" " * (1024 * 1024 + 1))
    report = read_details(meeting)
    assert report.notes and report.ended == UNKNOWN
    assert "The language used to write it down couldn’t be read." in report.notes
    session.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"PRIVATE_PRINT_SENTINEL"}')
    session.symlink_to(outside)
    assert "PRIVATE_PRINT_SENTINEL" not in read_details(meeting).plain_text()
    assert session.is_symlink()


def test_large_transcripts_keep_the_header_language_without_loading_words(meetings):
    meeting = meetings["microphone"]
    path = meeting.directory / "transcript.json"
    with path.open("w") as stream:
        stream.write('{"format":"cassini.words.v1","language":"en","items":[')
        stream.write('"large-unused-word-array",' * 100000)
        stream.write('"last"]}')
    report = read_details(meeting)
    assert dict(report.writing)["Language"] == "English"


def test_fractional_known_offsets_are_displayed_rather_than_losing_the_span(meetings):
    meeting = meetings["microphone"]
    track = replace(meeting.tracks[0], start_offset_ms=0.5, frames=8000)
    rows = timeline(replace(meeting, tracks=(track,), duration_ms=1000), {})
    assert len(rows) == 1 and (rows[0].start_ms, rows[0].end_ms) == (0, 1000)


def test_recovered_media_with_unknown_provenance_is_not_called_browser_audio(meetings):
    meeting = meetings["no-events"]
    track = replace(meeting.tracks[0], kind="media", start_offset_ms=None)
    report = read_details(replace(meeting, capture_source="recovered", tracks=(track,)))
    assert len(report.spans) == 1
    assert report.spans[0].source == "Audio source not recorded"
    assert report.spans[0].time == "Timing not recorded"


def test_gap_frames_without_a_saved_rate_never_claim_zero_seconds(meetings):
    meeting = meetings["microphone"]
    path = meeting.directory / "session.json"
    session = json.loads(path.read_text())
    session["tracks"][0]["gapFrames"] = 4000
    session["tracks"][0].pop("sampleRate")
    write_json(path, session)
    assert dict(read_details(meeting).health)["Gaps"] == "4,000 frames · duration not recorded"


def test_live_writing_facts_wait_for_the_recording_to_finish(meetings):
    report = read_details(replace(meetings["fallback"], lifecycle="recording"))
    assert all(value == "After the recording" for _name, value in report.writing)


def test_native_system_sound_does_not_claim_a_browser_was_missing(meetings):
    meeting = meetings["fallback"]
    native = replace(meeting, capture_source="gnome")
    session = json.loads((meeting.directory / "session.json").read_text())
    session["events"] = [dict(type="othersSource", atMs=0, source="system", reason="missing")]
    native = replace(native, tracks=tuple(t for t in native.tracks if t.kind in ("microphone", "system")))
    assert all("Browser" not in span.reason and "browser" not in span.reason for span in timeline(native, session))


def test_native_unavailable_health_fields_are_not_shown(meetings):
    health = dict(read_details(meetings["no-events"]).health)
    assert set(health) == {"Microphone offset"}
    # Actual saved counters remain useful, even for native captures.
    assert "Gaps" in dict(read_details(meetings["microphone"]).health)


def test_new_writing_facts_do_not_get_inferred_for_older_meetings(meetings):
    values = dict(read_details(meetings["no-events"]).writing)
    assert values["Time taken"] == values["Speaker separation"] == UNKNOWN
    values = dict(read_details(meetings["microphone"]).writing)
    assert values["Time taken"] == "4.217 s" and values["Speaker separation"] == "Tell speakers apart was not installed"


def test_manual_end_fact_works_without_a_new_session_event(meetings):
    meeting = meetings["no-events"]
    updated = replace(
        meeting, metadata=dict(meeting.metadata, recordingDetails=dict(end=dict(reason="user", atMs=198000)))
    )
    assert read_details(updated).ended == "Stopped by you · 00:03:18"
    unknown_time = replace(updated, metadata=dict(meeting.metadata, recordingDetails=dict(end=dict(reason="user"))))
    assert read_details(unknown_time).ended == "Stopped by you"
