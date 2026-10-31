# SPDX-License-Identifier: GPL-3.0-or-later
"""S-6 append-only source state and sync/rename crash boundaries."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cassini_talk.recording_session import ProtocolError, RecordingSession, load_session
from cassini_talk.session_events import append_events, meeting_sources, read_events, source_state


def initial():
    return [
        dict(atMs=0, type="othersSource", source="browser", reason="start"),
        dict(atMs=0, type="selection", me=True, others=True, reason="start"),
    ]


def test_shared_s6_fixture_library_and_handwritten_timeline_states(tmp_path):
    from cassini_talk.library import LibraryScanner

    fixture = Path(__file__).parent / "ux/session-events"
    subprocess.run([sys.executable, str(fixture / "make.py"), str(tmp_path)], check=True, timeout=5)
    expected = json.loads((fixture / "expected.json").read_text())
    (manifest,) = tmp_path.glob("Recording-*/session.json")
    before = manifest.read_bytes()
    value = load_session(manifest)
    for row in expected["states"]:
        current = source_state([e for e in value["events"] if e["atMs"] <= row["atMs"]])
        assert dict(
            me=current.me,
            others=current.others,
            source=current.source,
            reason=current.reason,
            sinceMs=current.since_ms,
            fallback=current.fallback,
        ) == {k: v for k, v in row.items() if k != "atMs"}
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert meeting.duration_ms == expected["durationMs"] and meeting.error is None
    assert len(meeting.capture_events) == 5
    assert meeting_sources(meeting).me and not meeting_sources(meeting).others
    assert not meeting_sources(meeting).fallback
    for track in meeting.tracks:
        assert ("me" if track.kind in ("microphone", "local") else "others") == expected["trackRoles"][track.id]
        assert track.display_end_ms == expected["trackEndsMs"][track.id]
    assert manifest.read_bytes() == before


def test_shared_browser_off_on_fixture_retains_one_speaker_and_each_span_offset(tmp_path):
    from cassini_talk.library import LibraryScanner
    from cassini_talk.transcription import prepare_tracks

    fixture = Path(__file__).parent / "ux/session-events"
    subprocess.run(
        [sys.executable, str(fixture / "make.py"), str(tmp_path), "--case", "browser-off-on"], check=True, timeout=5
    )
    expected = json.loads((fixture / "browser-off-on.expected.json").read_text())
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    tracks, speakers = prepare_tracks(meeting)
    assert {t["id"]: t["speaker"] for t in tracks} == expected["trackSpeakers"]
    assert {t["id"]: t["offsetMs"] for t in tracks} == expected["trackOffsetsMs"]
    assert len(speakers) == expected["speakerCount"]
    assert meeting.duration_ms == expected["durationMs"]
    state = meeting_sources(meeting)
    assert (state.me, state.others, state.source, state.fallback) == (
        expected["me"],
        expected["others"],
        expected["source"],
        expected["fallback"],
    )


def test_pinned_reserved_restored_vector_is_read_without_rewriting(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "browser-protocol/conformance/session-events/restored.reader.json"
    vector = json.loads(fixture.read_text())
    path = tmp_path / "session.json"
    path.write_text(json.dumps(vector["session"]))
    before = path.read_bytes()
    events = load_session(path)["events"]
    assert list(read_events(events)) == events
    for row in vector["states"]:
        state = source_state([event for event in events if event["atMs"] <= row["atMs"]])
        assert dict(
            me=state.me,
            others=state.others,
            source=state.source,
            reason=state.reason,
            sinceMs=state.since_ms,
            fallback=state.fallback,
        ) == {key: value for key, value in row.items() if key != "atMs"}
    assert path.read_bytes() == before


def test_reserved_restored_remains_invalid_for_writing_and_selection():
    manifest = dict(events=initial())
    before = json.dumps(manifest)
    for event in (
        dict(atMs=1, type="othersSource", source="browser", reason="restored"),
        dict(atMs=1, type="selection", me=True, others=True, reason="restored"),
    ):
        with pytest.raises(ValueError):
            append_events(manifest, [event])
        assert json.dumps(manifest) == before
    assert read_events([event]) == ()  # selection.reason never reserves restored.


def test_reader_uses_newest_known_events_without_inferring_or_rewriting():
    events = initial() + [
        dict(atMs=754210, type="othersSource", source="system", reason="lost", future=True),
        dict(atMs=800000, type="future", source="browser"),
    ]
    before = json.dumps(events)
    state = source_state(events)
    assert state.me and state.others and state.fallback and state.since_ms == 754210
    assert state.source == "system" and state.reason == "lost"
    assert json.dumps(events) == before
    assert read_events(events)[-1]["future"] is True
    events += [
        dict(atMs=900000, type="selection", me=True, others=False, reason="user"),
        dict(atMs=900000, type="othersSource", source="none", reason="user"),
    ]
    state = source_state(events)
    assert state.me and not state.others and state.source == "none" and not state.fallback
    assert not source_state(None, default_source="system").fallback


@pytest.mark.parametrize(
    "invalid", [None, {}, [None], [dict(atMs=True, type="selection", me=True, others=True, reason="start")]]
)
def test_malformed_optional_reader_entries_do_not_damage_a_meeting(invalid):
    assert read_events(invalid) == ()
    assert not source_state(invalid).fallback


def test_writer_rejects_invalid_or_out_of_order_appends_without_changes():
    manifest = dict(events=initial())
    before = json.dumps(manifest)
    for event in (
        dict(atMs=-1, type="othersSource", source="system", reason="lost"),
        dict(atMs=1, type="selection", me=False, others=False, reason="user"),
        dict(atMs=1, type="othersSource", source="microphone", reason="lost"),
        dict(atMs=1, type="future"),
    ):
        with pytest.raises(ValueError):
            append_events(manifest, [event])
        assert json.dumps(manifest) == before
    append_events(manifest, [dict(atMs=2, type="othersSource", source="system", reason="lost")])
    with pytest.raises(ValueError, match="out-of-order"):
        append_events(manifest, [dict(atMs=1, type="selection", me=True, others=False, reason="user")])
    manifest["events"] *= 334
    with pytest.raises(ValueError, match="1000"):
        append_events(manifest, initial())


def test_track_and_event_share_one_checkpoint_and_ended_pcm_cannot_grow(tmp_path):
    session = RecordingSession(tmp_path)
    with session.change(initial()):
        session.add_track("remote", "Other", "remote", 48000, 1)
    session.append("remote", b"\x01\x00" * 4800, 754110)
    old = json.loads((session.directory / "session.json").read_text())
    with session.change([dict(atMs=754210, type="othersSource", source="system", reason="lost")]):
        session.end_track("remote")
        session.add_track("system", "The others", "system", 48000, 1)
        session.manifest["tracks"][-1]["startOffsetMs"] = 754210
        assert json.loads((session.directory / "session.json").read_text()) == old
    saved = load_session(session.directory / "session.json")
    assert saved["events"][-1]["source"] == "system"
    assert saved["tracks"][0]["ended"] and saved["tracks"][0]["endOffsetMs"] == 754210
    assert saved["tracks"][1]["startOffsetMs"] == 754210
    before = (session.directory / saved["tracks"][0]["file"]).read_bytes()
    with pytest.raises(ProtocolError, match="ended"):
        session.append("remote", b"\x02\x00" * 480, 754210)
    assert (session.directory / saved["tracks"][0]["file"]).read_bytes() == before
    session.finish()


def test_failed_change_rolls_back_metadata_and_unused_new_wav(tmp_path):
    session = RecordingSession(tmp_path)
    before = (session.directory / "session.json").read_bytes()
    with pytest.raises(RuntimeError, match="source refused"), session.change(initial()):
        session.add_track("new", "Other", "system", 48000, 1)
        raise RuntimeError("source refused")
    assert not session.writers and session.manifest["tracks"] == [] and "events" not in session.manifest
    assert (session.directory / "session.json").read_bytes() == before
    assert not list(session.directory.glob("track-*.wav"))
    session.finish()


@pytest.mark.parametrize("crash", ["before", "after"])
def test_process_crash_never_splits_event_from_track_checkpoint(tmp_path, crash):
    script = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[3])
from cassini_talk.recording_session import RecordingSession
session=RecordingSession(Path(sys.argv[1]))
replace=Path.replace
def crash_replace(path,target):
    if sys.argv[2] == 'before': os._exit(71)
    replace(path,target)
    os._exit(72)
Path.replace=crash_replace
with session.change([dict(atMs=754210,type='othersSource',source='system',reason='lost')]):
    session.add_track('fallback','The others','system',48000,1)
    session.manifest['tracks'][-1]['startOffsetMs']=754210
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), crash, str(Path(__file__).resolve().parents[1] / "src")],
        check=False,
        timeout=5,
    )
    assert completed.returncode == (71 if crash == "before" else 72)
    (manifest,) = tmp_path.glob("Recording-*/session.json")
    value = json.loads(manifest.read_text())
    assert bool(value.get("events")) == bool(value["tracks"]) == (crash == "after")
    if crash == "after":
        assert value["events"][0]["atMs"] == value["tracks"][0]["startOffsetMs"] == 754210
        assert (manifest.parent / value["tracks"][0]["file"]).is_file()


def test_failed_post_rename_durability_restores_working_state_without_deleting_referenced_wavs(tmp_path, monkeypatch):
    import os
    import stat

    session = RecordingSession(tmp_path)
    with session.change(initial()):
        session.add_track("remote", None, "remote", 48000, 1)
    session.append("remote", b"\x01\x00" * 48, 0)
    old = json.loads(json.dumps(session.manifest))
    original, failed = os.fsync, []

    def directory_barrier(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed.append(True)
            raise OSError("directory barrier failed after rename")
        original(fd)

    monkeypatch.setattr(os, "fsync", directory_barrier)
    with (
        pytest.raises(OSError, match="after rename"),
        session.change([dict(atMs=10, type="othersSource", source="system", reason="lost")]),
    ):
        session.end_track("remote")
        session.add_track("system", None, "system", 48000, 1)
    assert session.manifest == old
    saved = json.loads((session.directory / "session.json").read_text())
    assert saved == old
    assert all((session.directory / t["file"]).is_file() for t in saved["tracks"])
    assert set(session.writers) == {"remote"}
    # The old source still accepts PCM and the proposed source/event is absent.
    session.append("remote", b"\x02\x00" * 48, 1)
    session.finish(reason="user")
    assert session.manifest["tracks"][0]["frames"] == 96
