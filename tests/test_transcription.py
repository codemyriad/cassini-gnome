# SPDX-License-Identifier: GPL-3.0-or-later
"""Queue/metadata behavior with an injected child; real inference has its own CI goldens."""

import hashlib
import json
import threading
import time
from pathlib import Path

from test_library import capture, write_metadata

from cassini_viewer.library import LibraryScanner
from cassini_viewer.meeting_store import read_metadata, update_metadata
from cassini_viewer.transcription import TranscriptionQueue


def until(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    assert check(), "Queue did not reach expected state"


def capture_hashes(session):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in session.directory.iterdir()
        if p.name == "session.json" or p.suffix == ".wav"
    }


def test_fifo_queue_marks_waiting_job_queued_and_preserves_capture_and_user_metadata(tmp_path):
    sessions = [capture(tmp_path), capture(tmp_path)]
    write_metadata(sessions[0].directory, futureMember={"keep": True}, title="Before", titleSource="user")
    before = [capture_hashes(s) for s in sessions]
    entered, release = threading.Event(), threading.Event()
    calls, finished, states = [], [], []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            calls.append(Path(tracks[0]["path"]).parent)
            if len(calls) == 1:
                entered.set()
                assert release.wait(3)
            notify(dict(type="planned", etaMinutes=1))
            for order, track in enumerate(tracks):
                notify(
                    dict(
                        type="progress",
                        trackId=track["id"],
                        trackOrder=order,
                        language="en",
                        etaMinutes=0,
                        words=[dict(text="word" + str(order), startMs=100, endMs=200)],
                    )
                )
            return dict(diagnostics=[])

    def changed(directory):
        states.append((directory, read_metadata(directory)["transcript"]["status"]))

    queue = TranscriptionQueue(changed, finished.append, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(sessions[0].directory)
        until(entered.is_set)
        assert not queue.enqueue(sessions[0].directory)
        assert queue.enqueue(sessions[1].directory)
        until(lambda: (sessions[1].directory / "meeting.json").exists())
        until(lambda: read_metadata(sessions[1].directory)["transcript"]["status"] == "queued")
        update_metadata(sessions[0].directory, title="Edited while writing", notes=[dict(atMs=0, text="Keep this")])
        release.set()
        until(lambda: len(finished) == 2)
        assert calls == [s.directory for s in sessions]
        assert before == [capture_hashes(s) for s in sessions]
        metadata = read_metadata(sessions[0].directory)
        assert metadata["title"] == "Edited while writing" and metadata["futureMember"] == {"keep": True}
        assert metadata["notes"] == [dict(atMs=0, text="Keep this")]
        assert metadata["transcript"]["status"] == "done" and metadata["etaMinutes"] is None
        words = json.loads((sessions[0].directory / "transcript.json").read_text())["items"]
        assert [w["speaker"] for w in words] == ["me", "track-others"], "Equal-time ties follow source track order"
        meeting = next(m for m in LibraryScanner(tmp_path).scan().meetings if m.directory == sessions[0].directory)
        assert meeting.speaker_names == ("Me", "Speaker 2") and meeting.speakers[0].provenance == "source"
        for session in sessions:
            assert [state for directory, state in states if directory == session.directory][:2] == ["queued", "running"]
    finally:
        release.set()
        queue.close()


def test_worker_failure_becomes_retryable_without_changing_audio(tmp_path):
    session = capture(tmp_path)
    before = capture_hashes(session)
    finished = []

    class Client:
        fail = True
        closed = 0

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def close(self):
            Client.closed += 1

        def process_tracks(self, tracks, notify):
            if Client.fail:
                Client.fail = False
                raise RuntimeError("synthetic worker crash")
            notify(dict(type="planned", etaMinutes=0))
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, finished.append, client_factory=Client, bundle=tmp_path)
    try:
        queue.enqueue(session.directory)
        until(lambda: len(finished) == 1)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "failed" and metadata["error"]["id"] == "transcribe-failed"
        assert Client.closed == 1
        assert queue.enqueue(session.directory)
        until(lambda: len(finished) == 2)
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
        assert capture_hashes(session) == before
    finally:
        queue.close()


def test_newer_metadata_is_not_overwritten_and_unsafe_metadata_is_not_followed(tmp_path):
    session = capture(tmp_path)
    path = session.directory / "meeting.json"
    write_metadata(session.directory, version=2)
    before = path.read_bytes()
    finished = []
    queue = TranscriptionQueue(lambda _: None, finished.append, bundle=tmp_path)
    try:
        queue.enqueue(session.directory)
        until(lambda: bool(finished))
        assert path.read_bytes() == before and not (session.directory / "transcript.json").exists()
        path.unlink()
        target = tmp_path / "outside.json"
        target.write_bytes(before)
        path.symlink_to(target)
        queue.enqueue(session.directory)
        until(lambda: len(finished) == 2)
        assert target.read_bytes() == before and path.is_symlink()
    finally:
        queue.close()


def test_broker_poll_lag_never_starts_recognition_before_capture_stop(tmp_path):
    from cassini_viewer.transcription import should_auto_write

    session = capture(tmp_path, completed=False)
    try:
        before = LibraryScanner(tmp_path).scan().meetings[0]
        assert before.lifecycle == "saved" and before.capture_state == "recording"
        assert not should_auto_write(before, {})
        session.finish()
        after = LibraryScanner(tmp_path).scan().meetings[0]
        assert should_auto_write(after, {before.key: (before.lifecycle, before.capture_state)})
        assert not should_auto_write(after, None), "Historical captures wait for the user action"
    finally:
        session.finish()
