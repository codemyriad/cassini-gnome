# SPDX-License-Identifier: GPL-3.0-or-later
"""Real finalized WAV/manifest boundaries, no inferred historical end reasons."""

import json

import pytest

from cassini_viewer.recording_session import BrowserHost, ProtocolError, RecordingSession, WAVWriter
from cassini_viewer.session_events import END_REASONS, append_events, read_events, source_state


@pytest.mark.parametrize("reason", sorted(END_REASONS))
def test_end_is_single_durable_checkpoint_after_all_pcm_and_events(tmp_path, reason):
    now = [10.0]
    session = RecordingSession(tmp_path, clock=lambda: now[0])
    with session.change([dict(type="othersSource", atMs=0, source="browser", reason="start")]):
        session.add_track("remote", None, "remote", 48000, 1)
    # The accepted PCM end wins even when Stop's clock is behind it.
    session.append("remote", b"\x01\x00" * 48, 900)
    session.finish(reason=reason, at_ms=800)
    path = session.directory / "session.json"
    saved = json.loads(path.read_text())
    assert saved["state"] == "completed"
    assert saved["tracks"][0]["ended"] and saved["tracks"][0]["endOffsetMs"] == 901
    assert saved["events"][-1] == dict(type="end", reason=reason, atMs=901)
    before = path.read_bytes()
    session.finish(reason="error", at_ms=1000)
    assert path.read_bytes() == before
    assert source_state(saved["events"]).source == "browser"
    assert read_events(saved["events"])[-1]["reason"] == reason
    with pytest.raises(ValueError):
        append_events(session.manifest, [dict(type="selection", atMs=902, me=True, others=True, reason="user")])


def test_finish_reserves_terminal_slot_and_rejects_change_without_mutation(tmp_path):
    session = RecordingSession(tmp_path)
    with session.change([dict(type="selection", atMs=0, me=True, others=True, reason="start")] * 999):
        session.add_track("remote", None, "remote", 48000, 1)
    before = (session.directory / "session.json").read_bytes()
    with (
        pytest.raises(ValueError, match="1000"),
        session.change([dict(type="othersSource", atMs=0, source="system", reason="lost")]),
    ):
        pytest.fail("No source change may run without its event slot")
    assert (session.directory / "session.json").read_bytes() == before
    session.append("remote", b"\x01\x00" * 48, 0)
    session.finish()
    assert len(session.manifest["events"]) == 1000
    assert session.manifest["events"][-1]["type"] == "end"


def test_audio_gap_warning_preserves_user_stop_but_io_failure_records_error(tmp_path, monkeypatch):
    session = RecordingSession(tmp_path)
    session.add_track("remote", None, "remote", 48000, 1)
    session.append("remote", b"\x01\x00" * 48, 0)
    session.append_native("remote", b"\x01\x00" * 48, 1001, gap_frames=48000)
    session.finish(reason="user")
    assert session.manifest["problemKind"] == "gap"
    assert session.manifest["events"][-1]["reason"] == "user"

    original = WAVWriter.close

    def failed(writer):
        original(writer)
        raise OSError("disk sync failed")

    monkeypatch.setattr(WAVWriter, "close", failed)
    session = RecordingSession(tmp_path)
    session.add_track("remote", None, "remote", 48000, 1)
    with pytest.raises(OSError, match="sync"):
        session.finish(reason="user")
    saved = json.loads((session.directory / "session.json").read_text())
    assert saved["state"] == "interrupted" and saved["events"][-1]["reason"] == "error"


def test_end_may_follow_pcm_at_the_legal_24_hour_input_offset(tmp_path):
    session = RecordingSession(tmp_path)
    session.add_track("remote", None, "remote", 48000, 1)
    session.append("remote", b"\x01\x00" * 48, 86400000)
    session.finish(reason="user", at_ms=0)
    assert session.manifest["events"][-1]["atMs"] == 86400001


@pytest.mark.parametrize("invalid", ["guess", None, [], {}, True])
def test_invalid_native_stop_reason_is_rejected_before_finalizing(tmp_path, invalid):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start"))
    with pytest.raises(ProtocolError, match="reason"):
        host.handle(dict(type="stop", reason=invalid))
    assert not host.session.finished and not host.stopped
    host.handle(dict(type="stop", reason="appQuit"))
    assert host.session.manifest["events"][-1]["reason"] == "appQuit"


def test_native_stop_without_reason_defaults_to_user_despite_prior_warning(tmp_path):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start"))
    host.handle(dict(type="stop", problem="Earlier input gap warning"))
    assert host.session.manifest["events"][-1]["reason"] == "user"
