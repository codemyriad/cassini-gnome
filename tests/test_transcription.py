# SPDX-License-Identifier: GPL-3.0-or-later
"""Queue/metadata behavior with an injected child; real inference has its own CI goldens."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest
from durable import DURABLE_BOUND, Changes, Finished, durable
from test_library import capture, write_metadata

from cassini_talk.library import LibraryScanner
from cassini_talk.meeting_store import read_metadata, update_metadata
from cassini_talk.transcription import TranscriptionQueue


@pytest.fixture(autouse=True)
def configured_library(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))


def until(check, bound=5):
    """Poll queue state that has no event to wait on; `bound` only limits it.

    A job's completion has one: wait on durable.Finished instead.
    """
    deadline = time.monotonic() + bound
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


def test_done_empty_transcript_can_be_written_again_through_the_normal_queue(tmp_path):
    from dataclasses import replace

    session = capture(tmp_path)
    from cassini_talk.transcription import REFERENCE

    write_metadata(session.directory, transcript=dict(status="done", reference=REFERENCE))
    (session.directory / "transcript.json").write_text(
        json.dumps(dict(format="cassini.words.v1", language="und", complete=True, items=[]))
    )
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert meeting.no_speech and meeting.can_write_down
    assert not replace(meeting, duration_ms=0).no_speech
    assert not replace(meeting, transcript="none").no_speech
    before = capture_hashes(session)
    completed = Finished()

    class Client:
        def __init__(self, *_args, **_kw):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(dict(type="planned", etaMinutes=0, segments=1))
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=100, text="Recovered")],
                )
            )
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, completed, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(session.directory)
        completed.wait(1)
        updated = LibraryScanner(tmp_path).scan().meetings[0]
        assert updated.transcript == "done" and [w.text for w in updated.words] == ["Recovered"]
        assert not updated.no_speech and not updated.can_write_down
        assert capture_hashes(session) == before
    finally:
        queue.close()


def test_fifo_queue_marks_waiting_job_queued_and_preserves_capture_and_user_metadata(tmp_path):
    sessions = [capture(tmp_path), capture(tmp_path)]
    write_metadata(sessions[0].directory, futureMember={"keep": True}, title="Before", titleSource="user")
    before = [capture_hashes(s) for s in sessions]
    entered, release = threading.Event(), threading.Event()
    calls, finished, states = [], Finished(), []

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
                # Held while the test queues the second job and commits two
                # metadata edits: 8 fsyncs before release.set().
                durable(release, "the test did not release the first job")
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

    queue = TranscriptionQueue(changed, finished, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(sessions[0].directory)
        durable(entered, "the first job did not start")
        assert not queue.enqueue(sessions[0].directory)
        assert queue.enqueue(sessions[1].directory)
        until(lambda: (sessions[1].directory / "meeting.json").exists())
        until(lambda: read_metadata(sessions[1].directory)["transcript"]["status"] == "queued")
        update_metadata(sessions[0].directory, title="Edited while writing", notes=[dict(atMs=0, text="Keep this")])
        update_metadata(
            sessions[1].directory, speakers=[dict(id="me", name="Named while queued", provenance="user", colourIndex=0)]
        )
        release.set()
        finished.wait(2, exactly=True)
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
    finished = Finished()
    successful = []

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

    queue = TranscriptionQueue(
        lambda _: None, finished, client_factory=Client, bundle=tmp_path, completed=successful.append
    )
    try:
        queue.enqueue(session.directory)
        finished.wait(1, exactly=True)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "failed" and metadata["error"]["id"] == "transcribe-failed"
        assert successful == []
        assert Client.closed == 1
        assert queue.enqueue(session.directory)
        finished.wait(2, exactly=True)
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
        assert successful == ["whistle"]
        assert capture_hashes(session) == before
    finally:
        queue.close()


def test_a_failed_write_down_keeps_its_partial_words_unreferenced_until_try_again(tmp_path):
    """D67.2: failed shows no words because the file says so; the snapshot waits for Try again."""
    session = capture(tmp_path)
    finished, visible = Finished(), []

    class Client:
        fail = True

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            texts = ["Partial", "words"] if Client.fail else ["Partial", "words", "again"]
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=1,
                    words=[dict(startMs=i * 100, endMs=i * 100 + 90, text=t) for i, t in enumerate(texts)],
                )
            )
            if Client.fail:
                Client.fail = False
                raise RuntimeError("synthetic worker crash after partial words")
            return dict(diagnostics=[])

    def changed(directory):
        meeting = LibraryScanner(tmp_path).scan().meetings[0]
        visible.append((meeting.transcript, len(meeting.words)))

    queue = TranscriptionQueue(changed, finished, client_factory=Client, bundle=tmp_path)
    try:
        queue.enqueue(session.directory)
        finished.wait(1)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "failed" and metadata["transcript"]["reference"] is None
        assert metadata["error"] == dict(id="transcribe-failed", action="Try again")
        # The partial body is kept, unreferenced, and the Library shows the file as stored: no words.
        body = json.loads((session.directory / "transcript.json").read_text())
        assert [item["text"] for item in body["items"]] == ["Partial", "words"]
        meeting = LibraryScanner(tmp_path).scan().meetings[0]
        assert (meeting.transcript, meeting.words, meeting.quote_line) == ("failed", (), None)
        visible.clear()
        # Try again references the snapshot once more: it is visible while queued.
        assert queue.enqueue(session.directory)
        finished.wait(2)
        assert ("queued", 2) in visible, visible
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "done" and metadata["transcript"]["reference"] is not None
        assert len(LibraryScanner(tmp_path).scan().meetings[0].words) == 3
    finally:
        queue.close()


def test_a_shorter_failing_try_again_keeps_the_longer_snapshot(tmp_path):
    """D67.2: Try again retains the preserved snapshot until recomputation catches up, failing or not."""
    session = capture(tmp_path)
    finished = Finished()
    attempts = [["One", "two", "three"], ["One"]]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            texts = attempts.pop(0)
            words = [dict(startMs=i * 100, endMs=i * 100 + 90, text=t) for i, t in enumerate(texts)]
            notify(
                dict(type="progress", trackId=tracks[0]["id"], trackOrder=0, language="en", etaMinutes=1, words=words)
            )
            raise RuntimeError("synthetic worker crash after partial words")

    queue = TranscriptionQueue(lambda _: None, finished, client_factory=Client, bundle=tmp_path)
    try:
        for attempt in (1, 2):
            assert queue.enqueue(session.directory)
            finished.wait(attempt)
            metadata = read_metadata(session.directory)
            assert metadata["transcript"]["status"] == "failed" and metadata["transcript"]["reference"] is None
            body = json.loads((session.directory / "transcript.json").read_text())
            assert [item["text"] for item in body["items"]] == ["One", "two", "three"], attempt
        assert not attempts
    finally:
        queue.close()


def test_newer_metadata_is_not_overwritten_and_unsafe_metadata_is_not_followed(tmp_path):
    session = capture(tmp_path)
    path = session.directory / "meeting.json"
    write_metadata(session.directory, version=2)
    before = path.read_bytes()
    finished = Finished()
    queue = TranscriptionQueue(lambda _: None, finished, bundle=tmp_path)
    try:
        queue.enqueue(session.directory)
        finished.wait(1)
        assert path.read_bytes() == before and not (session.directory / "transcript.json").exists()
        path.unlink()
        target = tmp_path / "outside.json"
        target.write_bytes(before)
        path.symlink_to(target)
        queue.enqueue(session.directory)
        finished.wait(2, exactly=True)
        assert target.read_bytes() == before and path.is_symlink()
    finally:
        queue.close()


def test_broker_poll_lag_never_starts_recognition_before_capture_stop(tmp_path):
    from cassini_talk.transcription import should_auto_write

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
    from cassini_talk.transcription import prepare_tracks

    session = capture(tmp_path)
    (session.directory / "session.json").write_bytes(b"{bad")
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    tracks, speakers = prepare_tracks(meeting)
    assert len(speakers) == len(tracks) == 2
    assert all(s["name"] is None and s["provenance"] == "unnamed" for s in speakers.values())
    assert all(t["offsetMs"] is None for t in tracks)


def test_browser_span_identity_uses_utf8_bound_and_never_labels(tmp_path):
    from cassini_talk.recording_session import RecordingSession
    from cassini_talk.transcription import prepare_tracks

    session = RecordingSession(tmp_path)
    identities = ["", "é" * 101, "é" * 100, "é" * 100]
    with session.change([]):
        for index, source in enumerate(identities):
            session.add_track(f"remote-{index}", "The same browser label", "remote", 48000, 1)
            session.manifest["tracks"][-1]["sourceTrackId"] = source
            session.append(f"remote-{index}", b"\x01\x00" * 480, index * 100)
    session.finish()
    before = (session.directory / "session.json").read_bytes()
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    tracks, speakers = prepare_tracks(meeting)
    assert len(tracks) == 4 and len(speakers) == 3
    assert tracks[0]["speaker"] != tracks[1]["speaker"]
    assert tracks[2]["speaker"] == tracks[3]["speaker"]
    assert len([d for d in meeting.diagnostics if d.code == "source-track-identity-invalid"]) == 2
    assert all(s["name"] is None and s["provenance"] == "unnamed" for s in speakers.values())
    assert (session.directory / "session.json").read_bytes() == before


def test_close_cancels_real_busy_child_without_blocking_or_failing_partial_work(tmp_path, monkeypatch):
    from test_whistle_spike import child_client

    sessions = [capture(tmp_path) for _ in range(3)]
    children, finished, changes = [], Finished(), Changes()
    owner_leaving, release_owner, owner_timed_out = (threading.Event() for _ in range(3))

    def factory(bundle, **kwargs):
        client = child_client(
            tmp_path,
            monkeypatch,
            """import json,signal,sys
print(json.dumps(dict(type='ready')),flush=True)
request=json.loads(sys.stdin.readline())
track=request['tracks'][0]
print(json.dumps(dict(type='progress',trackId=track['id'],trackOrder=0,language='en',etaMinutes=1,
words=[dict(text='Preserved',startMs=100,endMs=200)])),flush=True)
signal.pause()
""",
            timeout=2 * DURABLE_BOUND,
        )
        process_tracks = client.process_tracks

        def held_process_tracks(*args):
            try:
                return process_tracks(*args)
            finally:
                owner_leaving.set()
                if not release_owner.wait(DURABLE_BOUND):
                    owner_timed_out.set()

        monkeypatch.setattr(client, "process_tracks", held_process_tracks)
        children.append(client)
        return client

    queue = TranscriptionQueue(changes, finished, client_factory=factory, bundle=tmp_path)
    try:
        for session in sessions:
            assert queue.enqueue(session.directory)
        first = sessions[0].directory
        changes.wait(
            lambda: (first / "transcript.json").exists() and b"Preserved" in (first / "transcript.json").read_bytes(),
            "the first job did not publish its partial words",
        )
        changes.wait(
            lambda: all(read_metadata(s.directory)["transcript"]["status"] == "queued" for s in sessions[1:]),
            "the waiting jobs did not publish their queue state",
        )
        partial = (first / "transcript.json").read_bytes()
        child = children[0].process
        assert child is not None and child.poll() is None and not owner_leaving.is_set()
        start = time.thread_time()
        queue.close()
        caller_cpu = time.thread_time() - start
        if os.environ.get("CASSINI_PERF_GATE") == "1":
            assert caller_cpu < 0.5, "close used too much caller CPU"
        durable(owner_leaving, "the active recognition worker observed cancellation")
        assert not owner_timed_out.is_set() and first in queue.pending, "close waited for the held recognition owner"
        release_owner.set()
        finished.wait(3, exactly=True)
        child.wait(timeout=DURABLE_BOUND)
        assert len(set(finished)) == 3 and not queue.pending
        assert read_metadata(first)["transcript"]["status"] == "running"
        assert (first / "transcript.json").read_bytes() == partial
        assert all(read_metadata(s.directory)["transcript"]["status"] == "queued" for s in sessions[1:])
        assert not queue.enqueue(first)
    finally:
        release_owner.set()
        queue.close()
    # A queued resume must not erase already published words while it waits.
    resumed = TranscriptionQueue(lambda _: None, lambda _: None, bundle=tmp_path)
    try:
        assert resumed._mark_queued(first, True)
        assert (first / "transcript.json").read_bytes() == partial
        assert read_metadata(first)["transcript"]["status"] == "queued"
    finally:
        resumed.close()


def test_prepared_tracks_only_use_validated_identity_and_keep_ambiguous_sources_separate(tmp_path):
    from cassini_talk.transcription import prepare_tracks

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
    from cassini_talk.transcription import track_speaker

    for kind in ("tab", "media", "application", "unknown"):
        assert track_speaker(dict(id="source", kind=kind)) == ("track-source", None, "unnamed")
    assert track_speaker(dict(id="system", kind="system")) == ("others", "The others", "source")


def test_progress_keeps_speaker_edits_made_after_enqueue_and_between_segments(tmp_path):
    from cassini_talk.transcription import update_progress_metadata

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

    from cassini_talk import transcription

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "libc_ver", lambda: ("glibc", "2.42"))
    found = {"numpy", "onnxruntime"}
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name in found else None)
    monkeypatch.setattr(transcription, "onnxruntime_dir", lambda: None)
    bundle.mkdir(exist_ok=True)
    (bundle / "pins.json").write_text("{}")
    return transcription.unavailable_reason, found


def test_unavailable_reason_is_none_with_bundle_and_runtime_on_glibc_x86_64(monkeypatch, tmp_path):
    reason, _found = _available_platform(monkeypatch, tmp_path / "whistle")
    assert reason(tmp_path / "whistle") is None


def test_unavailable_reason_accepts_the_private_onnxruntime_without_a_system_one(monkeypatch, tmp_path):
    from cassini_talk import transcription

    reason, found = _available_platform(monkeypatch, tmp_path / "whistle")
    found.discard("onnxruntime")
    monkeypatch.setattr(transcription, "onnxruntime_dir", lambda: tmp_path / "python")
    assert reason(tmp_path / "whistle") is None
    found.discard("numpy")
    assert reason(tmp_path / "whistle") == "Python cannot find numpy"


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
    from cassini_talk.meeting_store import atomic_json
    from cassini_talk.transcription import REFERENCE

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
    finished, visible = Finished(), []

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

    queue = TranscriptionQueue(changed, finished, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(session.directory, resume=resume)
        durable(waiting, "the job did not reach recognition")
        assert (session.directory / "transcript.json").read_bytes() == before
        assert visible and all(count == 50 for state, count in visible if state in ("queued", "running")), visible
        release.set()
        finished.wait(1)
        result = json.loads((session.directory / "transcript.json").read_text())
        assert result["items"][0]["text"] == "Recomputed" and len(result["items"]) == 1
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
    finally:
        release.set()
        queue.close()


def test_enqueue_resume_while_prepare_is_in_flight_does_not_strand_queued_meeting(tmp_path):
    from cassini_talk.transcription import REFERENCE

    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status="queued", reference=REFERENCE))
    entered, release = threading.Event(), threading.Event()
    finished = Finished()
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            calls.append(len(tracks))
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=200, text="Let us review the quarterly budget today")],
                )
            )
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, finished, client_factory=Client, bundle=tmp_path)
    original_mark = queue._mark_queued
    first_pass = True

    def gated_mark(directory, resume):
        nonlocal first_pass
        if first_pass:
            first_pass = False
            entered.set()
            durable(release, "the test did not release in-flight prepare")
        return original_mark(directory, resume)

    queue._mark_queued = gated_mark
    try:
        assert queue.enqueue(session.directory, resume=False)
        durable(entered, "the first prepare pass did not start")
        # While a non-resume prepare is still scanning, a resume=True handoff arrives:
        # it must not add a second hold, and must not be lost when the first pass returns None.
        assert not queue.enqueue(session.directory, resume=True)
        release.set()
        finished.wait(1, exactly=True)
        assert calls == [2]
        updated = LibraryScanner(tmp_path).scan().meetings[0]
        assert updated.transcript == "done"
        assert [w.text for w in updated.words] == ["Let us review the quarterly budget today"]
    finally:
        release.set()
        queue.close()


def test_enqueue_resume_right_after_prepared_releases_lock_never_loses_resume_handoff(tmp_path):
    from cassini_talk.transcription import REFERENCE

    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status="queued", reference=REFERENCE))
    lock_released, allow_after_release = threading.Event(), threading.Event()
    finished = Finished()
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            calls.append(len(tracks))
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=200, text="Let us review the quarterly budget today")],
                )
            )
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, finished, client_factory=Client, bundle=tmp_path)
    real_lock = queue.lock
    original_prepared = queue._prepared
    arm_gate = [True]
    watch_exit = [False]

    class GateLock:
        def acquire(self, *args, **kwargs):
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            return real_lock.__enter__()

        def __exit__(self, *exc):
            result = real_lock.__exit__(*exc)
            if watch_exit[0]:
                watch_exit[0] = False
                lock_released.set()
                durable(allow_after_release, "the test did not release _prepared after lock exit")
            return result

    def gated_prepared(directory, future):
        if arm_gate[0]:
            arm_gate[0] = False
            watch_exit[0] = True
        return original_prepared(directory, future)

    queue.lock = GateLock()
    queue._prepared = gated_prepared
    try:
        accepted = int(queue.enqueue(session.directory, resume=False))
        durable(lock_released, "_prepared did not reach its lock release")
        # Right after _prepared exits its lock for a None prepare result, a resume=True
        # handoff arrives before _prepared invokes completion callbacks: because pending
        # removal was atomic with the retry check, this enqueue is accepted immediately.
        resumed = queue.enqueue(session.directory, resume=True)
        assert resumed, "Pending removal was not atomic with the retry check under queue.lock"
        accepted += int(resumed)
        allow_after_release.set()
        finished.wait(accepted, exactly=True)
        assert calls == [2]
        updated = LibraryScanner(tmp_path).scan().meetings[0]
        assert updated.transcript == "done"
        assert [w.text for w in updated.words] == ["Let us review the quarterly budget today"]
    finally:
        allow_after_release.set()
        queue.close()


def test_stop_running_transcription_preserves_partial_words_and_cache_and_lets_queued_job_finish(tmp_path):
    from concurrent.futures import CancelledError

    first = capture(tmp_path)
    second = capture(tmp_path)
    finished = Finished()
    first_segment_emitted = threading.Event()
    stop_completed = threading.Event()
    second_started = threading.Event()
    client_instances = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.cancelled = threading.Event()
            self.closed = False
            self.recognition_cache = None
            client_instances.append(self)

        def start(self):
            return self

        def cancel(self):
            self.cancelled.set()

        def close(self):
            self.closed = True

        def process_tracks(self, tracks, notify):
            if Path(tracks[0]["path"]).parent == first.directory:
                if self.recognition_cache and Path(self.recognition_cache["path"]).is_file():
                    cached = json.loads(Path(self.recognition_cache["path"]).read_text())["segments"]
                    assert "a" * 64 in cached
                    notify(
                        dict(
                            type="progress",
                            trackId=tracks[0]["id"],
                            trackOrder=0,
                            language="en",
                            etaMinutes=1,
                            words=[dict(startMs=0, endMs=300, text="Opening")],
                        )
                    )
                    notify(
                        dict(
                            type="planning",
                            trackId=tracks[0]["id"],
                            cacheEntry=dict(
                                key="b" * 64,
                                text="remarks.",
                                words=[dict(startMs=400, endMs=800, text="remarks.")],
                                language="en",
                            ),
                        )
                    )
                    notify(
                        dict(
                            type="progress",
                            trackId=tracks[0]["id"],
                            trackOrder=0,
                            language="en",
                            etaMinutes=0,
                            words=[dict(startMs=400, endMs=800, text="remarks.")],
                        )
                    )
                    return dict(diagnostics=[], segments=2)
                notify(
                    dict(
                        type="planning",
                        trackId=tracks[0]["id"],
                        cacheEntry=dict(
                            key="a" * 64,
                            text="Opening",
                            words=[dict(startMs=0, endMs=300, text="Opening")],
                            language="en",
                        ),
                    )
                )
                notify(
                    dict(
                        type="progress",
                        trackId=tracks[0]["id"],
                        trackOrder=0,
                        language="en",
                        etaMinutes=2,
                        words=[dict(startMs=0, endMs=300, text="Opening")],
                    )
                )
                first_segment_emitted.set()
                durable(self.cancelled, "stop() did not cancel the active client")
                raise CancelledError()
            assert not self.cancelled.is_set(), "Queued job reused a cancelled client"
            second_started.set()
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=500, text="Second meeting finished.")],
                )
            )
            return dict(diagnostics=[], segments=1)

    queue = TranscriptionQueue(lambda _: None, finished, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(first.directory)
        assert queue.enqueue(second.directory)
        durable(first_segment_emitted, "first job never emitted its first segment")
        assert queue.stop(first.directory, done=stop_completed.set)
        durable(stop_completed, "stop() did not finish")
        finished.wait(2, exactly=True)
        assert second_started.is_set()
        assert client_instances[0].closed

        meetings = {m.directory: m for m in LibraryScanner(tmp_path).scan().meetings}
        stopped = meetings[first.directory]
        completed = meetings[second.directory]
        assert stopped.transcript == "none" and stopped.can_write_down and stopped.error is None
        assert stopped.eta_minutes is None
        assert [w.text for w in stopped.words] == ["Opening"]
        cache_path = first.directory / ".recognition-cache.json"
        assert cache_path.is_file()
        assert set(json.loads(cache_path.read_text())["segments"]) == {"a" * 64}
        assert completed.transcript == "done"
        assert [w.text for w in completed.words] == ["Second meeting finished."]

        # Resuming the stopped meeting reuses the saved recognition cache and cleans it up on completion.
        assert queue.enqueue(first.directory)
        finished.wait(3, exactly=True)
        resumed = {m.directory: m for m in LibraryScanner(tmp_path).scan().meetings}[first.directory]
        assert resumed.transcript == "done"
        assert [w.text for w in resumed.words] == ["Opening", "remarks."]
        assert not cache_path.exists()
    finally:
        queue.close()


def test_stop_queued_and_published_library_v2_transcription(tmp_path, monkeypatch):
    import shutil
    from concurrent.futures import CancelledError

    from cassini_talk import portable
    from cassini_talk.library_publication import capture_parent
    from cassini_talk.library_v2 import finalize, mixdown

    def copy_plain_opus(_spans, dest, _choice, _cancel=None):
        shutil.copyfile(Path(__file__).parent / "conformance" / "opus" / "002-plain-audio.opus", dest)
        with open(dest, "rb") as stream:
            return portable.compute_audio_digest(stream)["sampleCount"]

    monkeypatch.setattr(mixdown, "total_samples", lambda _spans: 4800)
    monkeypatch.setattr(mixdown, "encode", copy_plain_opus)
    blocker = capture(tmp_path)
    managed = capture(capture_parent(tmp_path))
    assert finalize.finalize(tmp_path, managed.manifest["id"], zone="UTC") is not None
    published_session = managed.directory
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    queued_stopped = threading.Event()
    running_started = threading.Event()
    running_stopped = threading.Event()
    finished = Finished()

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.cancelled = threading.Event()

        def start(self):
            return self

        def cancel(self):
            self.cancelled.set()

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            session_dir = Path(tracks[0].get("sessionDirectory") or Path(tracks[0]["path"]).parent)
            if session_dir == blocker.directory:
                blocker_started.set()
                durable(release_blocker, "blocker was not released")
                return dict(diagnostics=[], segments=0)
            running_started.set()
            if not self.cancelled.is_set():
                notify(
                    dict(
                        type="planning",
                        trackId=tracks[0]["id"],
                        cacheEntry=dict(
                            key="c" * 64,
                            text="Published",
                            words=[dict(startMs=0, endMs=300, text="Published")],
                            language="en",
                        ),
                    )
                )
                notify(
                    dict(
                        type="progress",
                        trackId=tracks[0]["id"],
                        trackOrder=0,
                        language="en",
                        etaMinutes=1,
                        words=[dict(startMs=0, endMs=300, text="Published")],
                    )
                )
                durable(self.cancelled, "published running job was not cancelled")
                raise CancelledError()
            raise CancelledError()

    queue = TranscriptionQueue(lambda _: None, finished, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(blocker.directory)
        durable(blocker_started, "blocker never started")
        assert queue.enqueue(published_session)
        until(
            lambda: (
                next(m for m in LibraryScanner(tmp_path).scan().meetings if m.directory == published_session).transcript
                == "queued"
            )
        )
        assert queue.stop(published_session, done=queued_stopped.set)
        durable(queued_stopped, "queued v2 stop did not complete")
        stopped_queued = next(m for m in LibraryScanner(tmp_path).scan().meetings if m.directory == published_session)
        assert stopped_queued.transcript == "none" and stopped_queued.can_write_down
        release_blocker.set()
        finished.wait(2, exactly=True)

        # Now start running transcription on the published v2 meeting and stop mid-run.
        assert queue.enqueue(published_session)
        durable(running_started, "published v2 job never started")
        assert queue.stop(published_session, done=running_stopped.set)
        durable(running_stopped, "running v2 stop did not complete")
        finished.wait(3, exactly=True)
        stopped_running = next(m for m in LibraryScanner(tmp_path).scan().meetings if m.directory == published_session)
        assert stopped_running.transcript == "none" and stopped_running.can_write_down and stopped_running.error is None
        assert [w.text for w in stopped_running.words] == ["Published"]
        assert stopped_running.published_file is not None and stopped_running.published_file.is_file()
        embedded = portable.read_meeting(str(stopped_running.published_file))
        assert embedded.is_readable
        assert [w.text for w in portable.load_words(embedded, embedded.default_transcript_id)] == ["Published"]
        assert (published_session / ".recognition-cache.json").is_file()
    finally:
        release_blocker.set()
        queue.close()


def test_stop_during_voice_extraction_preserves_dirty_recognition_cache_and_words(tmp_path):
    from cassini_talk.voice_store import VoiceStore
    from cassini_talk.voices_logic import DIMENSION

    session = capture(tmp_path)
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    finished = Finished()
    voice_entered = threading.Event()
    stop_completed = threading.Event()
    voice_calls = []

    class ReadyModels:
        def request(self, _directory, *, origin="own"):
            return dict(modelPath=str(tmp_path / "model.onnx"), runtimePath=str(tmp_path), origin=origin)

        def measured(self, _rtf):
            pass

        def snapshot(self):
            return dict(ready=True)

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.cancelled = threading.Event()
            self.recognition_cache = None

        def start(self):
            return self

        def cancel(self):
            self.cancelled.set()

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            if self.recognition_cache and Path(self.recognition_cache["path"]).is_file():
                cached = json.loads(Path(self.recognition_cache["path"]).read_text())["segments"]
                assert set(cached) == {"1" * 64, "2" * 64}
            else:
                for key, start_ms, end_ms, word in (
                    ("1" * 64, 0, 250, "First"),
                    ("2" * 64, 300, 550, "Second"),
                ):
                    notify(
                        dict(
                            type="planning",
                            trackId=tracks[0]["id"],
                            cacheEntry=dict(
                                key=key,
                                text=word,
                                words=[dict(startMs=start_ms, endMs=end_ms, text=word)],
                                language="en",
                            ),
                        )
                    )
                    notify(
                        dict(
                            type="progress",
                            trackId=tracks[0]["id"],
                            trackOrder=0,
                            language="en",
                            etaMinutes=1,
                            words=[dict(startMs=start_ms, endMs=end_ms, text=word)],
                        )
                    )
                return dict(diagnostics=[], segments=2)
            for start_ms, end_ms, word in ((0, 250, "First"), (300, 550, "Second")):
                notify(
                    dict(
                        type="progress",
                        trackId=tracks[0]["id"],
                        trackOrder=0,
                        language="en",
                        etaMinutes=0,
                        words=[dict(startMs=start_ms, endMs=end_ms, text=word)],
                    )
                )
            return dict(diagnostics=[], segments=2)

        def voiceprints(self, _tracks, _options, heartbeat):
            voice_calls.append(len(voice_calls) + 1)
            if len(voice_calls) == 1:
                voice_entered.set()
                durable(self.cancelled, "stop() did not cancel voice extraction")
                heartbeat()
                raise AssertionError("cancelled voice extraction continued past heartbeat")
            vec = [0.0] * DIMENSION
            vec[0] = 1.0
            return {"others": dict(embedding=vec, seconds=8)}

    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        client_factory=Client,
        root=tmp_path,
        bundle=tmp_path,
        diarization_models=ReadyModels(),
        voice_models=ReadyModels(),
        voice_store=store,
    )
    try:
        assert queue.enqueue(session.directory)
        durable(voice_entered, "job never entered saved-voice extraction")
        assert queue.stop(session.directory, done=stop_completed.set)
        durable(stop_completed, "stop() during voice extraction did not complete")
        finished.wait(1, exactly=True)

        cache_path = session.directory / ".recognition-cache.json"
        assert cache_path.is_file()
        assert set(json.loads(cache_path.read_text())["segments"]) == {"1" * 64, "2" * 64}
        assert not (store.root / "meetings" / (session.directory.name + ".json")).exists()

        stopped = LibraryScanner(tmp_path).scan().meetings[0]
        assert stopped.transcript == "none" and stopped.can_write_down and stopped.error is None
        assert [w.text for w in stopped.words] == ["First", "Second"]

        assert queue.enqueue(session.directory)
        finished.wait(2, exactly=True)
        resumed = LibraryScanner(tmp_path).scan().meetings[0]
        assert resumed.transcript == "done"
        assert [w.text for w in resumed.words] == ["First", "Second"]
        assert not cache_path.exists()
        assert (store.root / "meetings" / (session.directory.name + ".json")).is_file()
    finally:
        queue.close()
