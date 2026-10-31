# SPDX-License-Identifier: GPL-3.0-or-later
import json
import struct

import pytest

from cassini_talk.recording_session import RecordingSession, load_session


@pytest.fixture
def recording(tmp_path):
    session = RecordingSession(tmp_path)
    session.add_track("source", "Known label", "remote", 48000, 1)
    session.append("source", b"\x01\x00" * 480, 200)
    session.finish()
    return session.directory


@pytest.mark.parametrize("metadata", [None, b"", b"{broken", b"[]"])
def test_recovery_is_read_only_and_does_not_invent_timing(recording, metadata):
    path = recording / "session.json"
    if metadata is None:
        path.unlink()
    else:
        path.write_bytes(metadata)
    before = {p.name: p.read_bytes() for p in recording.iterdir()}
    manifest = load_session(path)
    assert manifest["source"] == "recovered" and manifest["problemKind"] == "recovery"
    track = manifest["tracks"][0]
    assert track["label"] == "Recovered audio" and track["frames"] == 480
    assert "startOffsetMs" not in track
    assert before == {p.name: p.read_bytes() for p in recording.iterdir()}


def test_complete_physical_frames_override_stale_header(recording):
    path = next(recording.glob("track-*.wav"))
    with path.open("r+b") as audio:
        audio.seek(40)
        audio.write(struct.pack("<I", 0))
        audio.seek(0, 2)
        audio.write(b"\x00")  # Incomplete trailing PCM frame is ignored.
    assert load_session(recording / "session.json")["tracks"][0]["frames"] == 480


def test_symlink_escape_never_read(recording, tmp_path):
    audio = next(recording.glob("track-*.wav"))
    outside = tmp_path / "outside.wav"
    outside.write_bytes(audio.read_bytes())
    audio.unlink()
    audio.symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        load_session(recording / "session.json")
    (recording / "session.json").write_bytes(b"")
    with pytest.raises(ValueError, match="No readable"):
        load_session(recording / "session.json")


@pytest.mark.parametrize("change", [{"startOffsetMs": True}, {"sampleRate": 16000}, {"file": "../outside.wav"}])
def test_valid_json_cannot_bypass_track_validation(recording, change):
    path = recording / "session.json"
    manifest = json.loads(path.read_text())
    manifest["tracks"][0].update(change)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_session(path)
