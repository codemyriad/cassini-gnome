# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import math
import struct

import pytest

from cassini_talk.library import LibraryScanner
from cassini_talk.meeting_store import update_metadata
from cassini_talk.recording_details import SilenceClock, edit_recording, recording_observation
from cassini_talk.recording_session import RecordingSession


@pytest.fixture(autouse=True)
def configured_library(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))


def test_silence_requires_twenty_seconds_and_sound_resets_clock():
    clock = SilenceClock(100)
    assert not clock.update(0, 119.99)
    assert clock.update(0, 120)
    assert not clock.update(0.2, 121)
    assert not clock.update(0, 140.99)
    assert clock.update(0, 141)


@pytest.mark.parametrize("offset", [-2060, 4000])
def test_dc_offset_is_silent_but_offset_speech_is_sound_and_wavs_are_untouched(tmp_path, offset):
    from cassini_talk.recording_details import pcm_ac_level

    session = RecordingSession(tmp_path)
    session.add_track("mic", "Mic", "microphone", 48000, 1)
    pcm = struct.pack("<h", offset) * 48000
    assert pcm_ac_level(pcm) == 0
    assert SilenceClock(0).update(pcm_ac_level(pcm), 20)
    session.append("mic", pcm, 0)
    meeting = LibraryScanner(tmp_path).scan(capture_activity={session.manifest["id"]: "recording"}).meetings[0]
    path = session.directory / session.manifest["tracks"][0]["file"]
    before = path.read_bytes()
    state = recording_observation(meeting)
    assert state["levels"][0] == 0 and not state["recent_sound"]
    tone = b"".join(struct.pack("<h", offset + int(455 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(4800))
    assert pcm_ac_level(tone) > 5 / 60
    assert path.read_bytes() == before
    session.finish()


def test_ac_meter_uses_whole_hundred_ms_windows_across_buffer_boundaries():
    from cassini_talk.recording_details import ACMeter, pcm_ac_level

    pcm = b"".join(struct.pack("<h", -2060 + (455 if i % 2 else -455)) for i in range(4800))
    meter = ACMeter()
    assert not meter.feed(pcm[:2000]) and meter.level == 0
    assert not meter.feed(pcm[2000:8000]) and meter.level == 0
    assert meter.feed(pcm[8000:]) and meter.level == pcm_ac_level(pcm)
    assert not meter.pending
    assert meter.feed(struct.pack("<h", 4000) * 4800) and meter.level == 0


def test_live_edits_preserve_other_app_owned_fields_and_capture_bytes(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("mic", "Mic", "microphone", 48000, 1)
    session.append("mic", bytes(9600), 0)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.iterdir()}
    update_metadata(session.directory, future={"retained": True})
    edit_recording(session.directory, title="  Better name  ", notes=[dict(atMs=12345, text="A decision")])
    update_metadata(session.directory, transcript=dict(status="queued", reference=None))
    edit_recording(session.directory, title="Revised")
    value = json.loads((session.directory / "meeting.json").read_text())
    assert value["title"] == "Revised" and value["titleSource"] == "user"
    assert value["notes"] == [dict(atMs=12345, text="A decision")]
    assert value["future"] == {"retained": True} and value["transcript"]["status"] == "queued"
    assert all(
        hashlib.sha256((session.directory / name).read_bytes()).hexdigest() == sha for name, sha in before.items()
    )
    edit_recording(session.directory, title=" ")
    value = json.loads((session.directory / "meeting.json").read_text())
    assert value["title"] is None and value["titleSource"] == "meeting-window"
    session.finish()


def test_existing_notes_survive_title_and_progress_edits_and_still_export(tmp_path):
    from cassini_talk.export_model import markdown, model_projection

    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("mic", "Mic", "microphone", 16000, 1)
    session.append("mic", bytes(3200), 0)
    session.finish()
    notes = [dict(atMs=12345, text="Already typed", extra=dict(retain="exactly — ✓"))]
    update_metadata(session.directory, notes=notes)
    old = json.dumps(notes, ensure_ascii=False).encode()
    edit_recording(session.directory, title="A title edit")
    update_metadata(session.directory, transcript=dict(status="queued", reference=None), etaMinutes=1)
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    assert json.dumps(meeting.metadata["notes"], ensure_ascii=False).encode() == old
    assert list(meeting.notes) == notes
    assert markdown(model_projection(meeting, tz="UTC")).endswith("## Notes\n\n- 00:00:12 Already typed\n")


def test_new_meeting_metadata_keeps_empty_notes_without_an_editor(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    edit_recording(session.directory, title="A new title")
    assert json.loads((session.directory / "meeting.json").read_text())["notes"] == []
    session.finish()


def test_browser_source_observation_reads_real_pcm_without_writing(tmp_path):
    session = RecordingSession(tmp_path)
    for name, kind in [("mic", "local"), ("a", "remote"), ("b", "remote")]:
        session.add_track(name, name, kind, 48000, 1)
        pcm = (
            bytes(9600)
            if kind == "local"
            else b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(4800))
        )
        session.append(name, pcm, 0)
    meeting = LibraryScanner(tmp_path).scan(capture_activity={session.manifest["id"]: "recording"}).meetings[0]
    before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
    value = recording_observation(meeting)
    assert value["voices"] == 2
    assert value["levels"][0] == 0 and value["levels"][1] > 0.5
    assert all((session.directory / name).read_bytes() == data for name, data in before.items())
    session.finish()


def test_brief_microphone_sound_between_polls_resets_silence_even_when_meter_is_now_zero(tmp_path):
    session = RecordingSession(tmp_path)
    session.add_track("mic", "Mic", "microphone", 48000, 1)
    tone = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(4800))
    session.append("mic", tone + bytes(43200 * 2), 0)
    meeting = LibraryScanner(tmp_path).scan(capture_activity={session.manifest["id"]: "recording"}).meetings[0]
    observed = recording_observation(meeting)
    assert observed["levels"][0] == 0 and observed["recent_sound"]
    session.finish()


def test_targeted_browser_stop_does_not_stop_another_meeting(tmp_path, monkeypatch):
    from cassini_talk import browser_service
    from cassini_talk.capture_guard import stop_browser_captures

    paths = [str(tmp_path / "one"), str(tmp_path / "two")]
    calls = []

    def request(action="status", **values):
        if action == "stop":
            calls.append(values["id"])
        return dict(
            statuses=[
                dict(recording=str(index) not in calls, path=path, id=str(index)) for index, path in enumerate(paths)
            ],
            meetings=[dict(recording=True, path=path, id=str(index)) for index, path in enumerate(paths)],
        )

    monkeypatch.setattr(browser_service, "request", request)
    assert stop_browser_captures(paths[1]) == [paths[1]]
    assert calls == ["1"]


def test_long_live_title_is_bounded_without_dropping_batched_notes(tmp_path, monkeypatch):
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.recording_details import edit_recording

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = RecordingSession(tmp_path)
    notes = [dict(atMs=1, text="Retain this decision")]
    edit_recording(session.directory, title="x" * 1000, notes=notes)
    value = read_metadata(session.directory)
    assert value["title"] == "x" * 500 and value["notes"] == notes
    session.finish()


def test_default_capture_directory_is_canonical_under_symlinked_library(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "Library"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(link))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    session = RecordingSession(None)
    assert session.directory.parent == real / ".cassini/sessions"
    assert session.directory == session.directory.resolve()
    session.finish()


@pytest.mark.parametrize("expected", [False, True])
def test_live_source_failure_reaches_recording_observation_only_when_selected(tmp_path, expected):
    session = RecordingSession(tmp_path, source="chromium")
    session.add_track("mic", "Mic", "microphone", 48000, 1)
    session.append("mic", bytes(9600), 0)
    row = dict(sourceTrackId="guest", kind="remote", label="Guest", captureOutcome="failed", audioMs=0, trackIds=[])
    if not expected:
        row["captureExpected"] = False
    session.manifest["captureOutcome"] = dict(sources=[row])
    session.checkpoint()
    try:
        meeting = LibraryScanner(tmp_path).scan(capture_activity={session.manifest["id"]: "recording"}).meetings[0]
        before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
        assert recording_observation(meeting)["source_problem"] == expected
        assert {p.name: p.read_bytes() for p in session.directory.iterdir()} == before
        row["captureOutcome"] = "neverAudio"
        session.checkpoint()
        assert not recording_observation(meeting)["source_problem"], "Live startup is not a failure"
    finally:
        session.finish()
