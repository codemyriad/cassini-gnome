# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import math
import struct

from cassini_viewer.library import LibraryScanner
from cassini_viewer.meeting_store import update_metadata
from cassini_viewer.recording_details import SilenceClock, edit_recording, recording_observation
from cassini_viewer.recording_session import RecordingSession


def test_silence_requires_twenty_seconds_and_sound_resets_clock():
    clock = SilenceClock(100)
    assert not clock.update(0, 119.99)
    assert clock.update(0, 120)
    assert not clock.update(0.2, 121)
    assert not clock.update(0, 140.99)
    assert clock.update(0, 141)


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
