# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""Actual Stop -> encode -> journal -> file; startup retry with process leases."""

import hashlib
import json
import os
import threading
from concurrent.futures import CancelledError

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication tests require GStreamer", allow_module_level=True)

from library_v2_support import simple_session

from cassini_viewer import meeting_store, portable
from cassini_viewer.library import LibraryScanner
from cassini_viewer.library_location import capture_lease
from cassini_viewer.library_publication import capture_parent, publish_stopped, recover_completed, root_for
from cassini_viewer.library_v2 import finalize, journal
from cassini_viewer.recording_session import RecordingSession


def test_stop_publishes_capture_without_changing_saved_pcm(tmp_path):
    session = RecordingSession(capture_parent(tmp_path / "Library"), title="Actual capture")
    session.add_track("me", "Me", "microphone", 48000, 1)
    session.append("me", b"\x10\x00" * 4800, 0)
    session.finish()
    audio = next(session.directory.glob("*.wav"))
    before = hashlib.sha256(audio.read_bytes()).hexdigest()
    result = publish_stopped(session)
    (meeting,) = LibraryScanner(tmp_path / "Library").scan().meetings
    assert meeting.audio_file.name == result.name and meeting.id == session.manifest["id"]
    embedded = portable.read_meeting(str(meeting.audio_file))
    assert embedded.is_readable and portable.load_words(embedded, embedded.default_transcript_id) == []
    assert meeting.metadata["published"] is True
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == before
    assert not journal.pending(journal.Layout(tmp_path / "Library")).entries
    assert publish_stopped(session).already_published
    assert len(list((tmp_path / "Library").glob("*.opus"))) == 1


def test_never_publishes_live_capture_or_converts_explicit_legacy_parent(tmp_path):
    session = RecordingSession(tmp_path)
    with pytest.raises(ValueError, match="still active"):
        publish_stopped(session)
    session.finish()
    assert publish_stopped(session) is None
    assert not (tmp_path / ".cassini").exists()


def test_cancelled_publish_keeps_completed_session_and_startup_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "Library"
    root.mkdir()
    sid, directory = simple_session(root, 0.1)
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        list(recover_completed(root, cancel=stop))
    assert not list(root.glob("*.opus"))
    assert json.loads((directory / "session.json").read_text())["state"] == "completed"
    (result,) = recover_completed(root)
    assert result.error is None and result.published
    assert list(recover_completed(root)) == []
    assert meeting_store.read_metadata(directory)["published"]
    assert (root / result.published.name).exists()


def test_recovery_skips_live_saving_missing_published_and_legacy_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "Library"
    root.mkdir()
    live, live_dir = simple_session(root, 0.1)
    saving, _ = simple_session(root, 0.1)
    missing, _ = simple_session(root, 0.1)
    result = finalize.finalize(root, missing)
    (root / result.name).unlink()
    manifest = json.loads((live_dir / "session.json").read_text())
    manifest["state"] = "recording"
    (live_dir / "session.json").write_text(json.dumps(manifest))
    raw = RecordingSession(root)
    raw.finish()
    lease = capture_lease(saving)
    try:
        assert list(recover_completed(root)) == []
    finally:
        os.close(lease)
    (result,) = recover_completed(root)
    assert result.directory.name == "Recording-" + saving
    assert len(list(root.glob("*.opus"))) == 1
    assert not (raw.directory / "meeting.json").exists()
    assert root_for(raw.directory) is None
    assert live_dir.exists()


def test_recovery_reports_one_failure_and_continues_without_dropping_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    first, first_dir = simple_session(tmp_path, 0.1)
    second, _ = simple_session(tmp_path, 0.1)
    original = finalize.finalize

    def fail_one(root, sid, **kwargs):
        if sid == first:
            raise OSError("synthetic failed encode")
        return original(root, sid, **kwargs)

    monkeypatch.setattr(finalize, "finalize", fail_one)
    outcomes = list(recover_completed(tmp_path))
    assert len(outcomes) == 2
    assert [r for r in outcomes if r.error][0].directory == first_dir
    assert [r for r in outcomes if r.published][0].directory.name == "Recording-" + second
    assert next(first_dir.glob("*.wav")).exists()
    assert not meeting_store.read_metadata(first_dir).get("published")


def test_hidden_symlinks_are_rejected_without_mutation(tmp_path):
    library, outside = tmp_path / "Library", tmp_path / "outside"
    library.mkdir()
    outside.mkdir()
    (library / ".cassini").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, NotADirectoryError)):
        capture_parent(library)
    with pytest.raises(ValueError):
        list(recover_completed(library))
    assert list(outside.iterdir()) == []


def test_encoder_timeout_is_reported_once_instead_of_retrying_future_result_forever(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sid, directory = simple_session(tmp_path, 0.1)

    def timeout(*_args, **_kwargs):
        raise TimeoutError("synthetic encoder timeout")

    monkeypatch.setattr(finalize, "finalize", timeout)
    (result,) = recover_completed(tmp_path)
    assert result.directory == directory and isinstance(result.error, TimeoutError)
    assert not list(tmp_path.glob("*.opus"))
