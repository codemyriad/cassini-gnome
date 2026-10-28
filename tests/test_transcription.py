# SPDX-License-Identifier: GPL-3.0-or-later
"""Queue/metadata behavior with an injected child; real inference has its own CI goldens."""

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from test_library import capture, write_metadata

from cassini_viewer.library import LibraryScanner
from cassini_viewer.meeting_store import read_metadata, update_metadata
from cassini_viewer.transcription import TranscriptionQueue


@pytest.fixture(autouse=True)
def configured_library(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))


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
        update_metadata(
            sessions[1].directory, speakers=[dict(id="me", name="Named while queued", provenance="user", colourIndex=0)]
        )
        release.set()
        until(lambda: len(finished) == 2)
        assert calls == [s.directory for s in sessions]
        assert read_metadata(sessions[1].directory)["speakers"][0]["name"] == "Named while queued"
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


def test_salvaged_untyped_tracks_do_not_invent_capture_source_names(tmp_path):
    from cassini_viewer.transcription import prepare_tracks

    session = capture(tmp_path)
    (session.directory / "session.json").write_bytes(b"{bad")
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    tracks, speakers = prepare_tracks(meeting)
    assert len(speakers) == len(tracks) == 2
    assert all(s["name"] is None and s["provenance"] == "unnamed" for s in speakers.values())
    assert all(t["offsetMs"] is None for t in tracks)


def test_close_cancels_real_busy_child_without_blocking_or_failing_partial_work(tmp_path, monkeypatch):
    from test_whistle_spike import child_client

    sessions = [capture(tmp_path) for _ in range(3)]
    children, finished = [], []

    def factory(bundle, **kwargs):
        client = child_client(
            tmp_path,
            monkeypatch,
            """import json,sys,time
print(json.dumps(dict(type='ready')),flush=True)
request=json.loads(sys.stdin.readline())
track=request['tracks'][0]
print(json.dumps(dict(type='progress',trackId=track['id'],trackOrder=0,language='en',etaMinutes=1,
words=[dict(text='Preserved',startMs=100,endMs=200)])),flush=True)
time.sleep(30)
""",
        )
        children.append(client)
        return client

    queue = TranscriptionQueue(lambda _: None, finished.append, client_factory=factory, bundle=tmp_path)
    for session in sessions:
        assert queue.enqueue(session.directory)
    first = sessions[0].directory
    until(lambda: (first / "transcript.json").exists() and b"Preserved" in (first / "transcript.json").read_bytes())
    until(lambda: all((s.directory / "meeting.json").exists() for s in sessions))
    partial = (first / "transcript.json").read_bytes()
    child = children[0].process
    start = time.monotonic()
    queue.close()
    assert time.monotonic() - start < 0.5, "close waited for child inference on the calling thread"
    until(lambda: len(finished) == 3)
    until(lambda: child.poll() is not None)
    assert len(set(finished)) == 3 and not queue.pending
    assert read_metadata(first)["transcript"]["status"] == "running"
    assert (first / "transcript.json").read_bytes() == partial
    assert all(read_metadata(s.directory)["transcript"]["status"] == "queued" for s in sessions[1:])
    assert not queue.enqueue(first)
    # A queued resume must not erase already published words while it waits.
    resumed = TranscriptionQueue(lambda _: None, lambda _: None, bundle=tmp_path)
    try:
        assert resumed._mark_queued(first, True)
        assert (first / "transcript.json").read_bytes() == partial
        assert read_metadata(first)["transcript"]["status"] == "queued"
    finally:
        resumed.close()


def test_prepared_tracks_only_use_validated_identity_and_keep_ambiguous_sources_separate(tmp_path):
    from cassini_viewer.transcription import prepare_tracks

    session = capture(tmp_path)
    path = session.directory / "session.json"
    for changes in ("list", "identity", "duplicate", "wrong-types"):
        manifest = json.loads(json.dumps(session.manifest))
        if changes == "list":
            manifest = []
        elif changes == "identity":
            manifest["id"] = "invalid"
        elif changes == "duplicate":
            for track in manifest["tracks"]:
                track.update(id="duplicated", kind="remote")
        else:
            for track in manifest["tracks"]:
                track.update(id=123, kind={"untrusted": True})
        path.write_text(json.dumps(manifest))
        meeting = LibraryScanner(tmp_path).scan().meetings[0]
        # Subsequent raw changes must not influence the validated snapshot.
        path.write_text('["must not be read here"]')
        tracks, speakers = prepare_tracks(meeting)
        assert len(tracks) == len(speakers) == 2, changes
        assert len({t["id"] for t in tracks}) == 2
        assert all(s["provenance"] == "unnamed" and s["name"] is None for s in speakers.values())


def test_browser_mixed_sources_do_not_claim_system_sound_or_invent_people():
    from cassini_viewer.transcription import track_speaker

    for kind in ("tab", "media", "application", "unknown"):
        assert track_speaker(dict(id="source", kind=kind)) == ("track-source", None, "unnamed")
    assert track_speaker(dict(id="system", kind="system")) == ("others", "The others", "source")


def test_progress_keeps_speaker_edits_made_after_enqueue_and_between_segments(tmp_path):
    from cassini_viewer.transcription import update_progress_metadata

    session = capture(tmp_path)
    update_metadata(
        session.directory,
        speakers=[dict(id="me", name="Edited", provenance="user", colourIndex=0, futureLabel="retained")],
    )
    planned = dict(me=dict(id="me", name="Me", provenance="source", colourIndex=0))
    update_progress_metadata(session.directory, planned, etaMinutes=None)
    update_metadata(
        session.directory, speakers=[dict(id="me", name="Edited again", provenance="recognised", colourIndex=0)]
    )
    update_progress_metadata(session.directory, planned, etaMinutes=None)
    value = read_metadata(session.directory)
    assert value["speakers"] == [dict(id="me", name="Edited again", provenance="recognised", colourIndex=0)]


def _available_platform(monkeypatch, bundle):
    import importlib.util
    import platform
    import sys

    from cassini_viewer import transcription

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "libc_ver", lambda: ("glibc", "2.42"))
    found = {"numpy", "onnxruntime"}
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name in found else None)
    bundle.mkdir(exist_ok=True)
    (bundle / "pins.json").write_text("{}")
    return transcription.unavailable_reason, found


def test_unavailable_reason_is_none_with_bundle_and_runtime_on_glibc_x86_64(monkeypatch, tmp_path):
    reason, _found = _available_platform(monkeypatch, tmp_path / "whistle")
    assert reason(tmp_path / "whistle") is None


def test_unavailable_reason_names_each_missing_requirement(monkeypatch, tmp_path):
    import platform

    reason, found = _available_platform(monkeypatch, tmp_path / "whistle")
    found.discard("onnxruntime")
    assert reason(tmp_path / "whistle") == "Python cannot find onnxruntime"
    found.discard("numpy")
    assert reason(tmp_path / "whistle") == "Python cannot find numpy and onnxruntime"
    assert "not installed" in reason(tmp_path / "missing")
    monkeypatch.setattr(platform, "libc_ver", lambda: ("", ""))
    assert "needs glibc" in reason(tmp_path / "whistle")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert "x86_64, not linux aarch64" in reason(tmp_path / "whistle")


def test_unavailable_reason_defaults_to_the_configured_bundle(monkeypatch, tmp_path):
    reason, _found = _available_platform(monkeypatch, tmp_path / "whistle")
    monkeypatch.setenv("CASSINI_WHISTLE_BUNDLE", str(tmp_path / "whistle"))
    assert reason() is None
    monkeypatch.setenv("CASSINI_WHISTLE_BUNDLE", str(tmp_path / "elsewhere"))
    assert "not installed" in reason()


@pytest.mark.parametrize(("status", "resume"), [("failed", False), ("running", True)])
def test_retry_keeps_partial_words_until_recomputation_catches_up(tmp_path, status, resume):
    from cassini_viewer.meeting_store import atomic_json
    from cassini_viewer.transcription import REFERENCE

    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status=status, reference=REFERENCE))
    body = dict(
        format="cassini.words.v1",
        language="en",
        items=[dict(speaker="me", startMs=i * 100, endMs=i * 100 + 90, text=f"Preserved {i}") for i in range(50)],
    )
    atomic_json(session.directory / "transcript.json", body)
    before = (session.directory / "transcript.json").read_bytes()
    waiting, release = threading.Event(), threading.Event()
    finished, visible = [], []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=1,
                    words=[dict(startMs=0, endMs=90, text="Recomputed")],
                )
            )
            waiting.set()
            assert release.wait(3)
            return dict(diagnostics=[])

    def changed(directory):
        meeting = LibraryScanner(tmp_path).scan().meetings[0]
        visible.append((meeting.transcript, len(meeting.words)))

    queue = TranscriptionQueue(changed, finished.append, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(session.directory, resume=resume)
        until(waiting.is_set)
        assert (session.directory / "transcript.json").read_bytes() == before
        assert visible and all(count == 50 for state, count in visible if state in ("queued", "running")), visible
        release.set()
        until(lambda: bool(finished))
        result = json.loads((session.directory / "transcript.json").read_text())
        assert result["items"][0]["text"] == "Recomputed" and len(result["items"]) == 1
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
    finally:
        release.set()
        queue.close()
