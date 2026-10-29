# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognition is useful even when optional voice work fails or is cancelled."""

import json
from concurrent.futures import CancelledError

import pytest
from test_transcription import until
from test_voice_store import embedding

from cassini_viewer.library import LibraryScanner
from cassini_viewer.meeting_store import read_metadata, update_metadata
from cassini_viewer.recording_session import RecordingSession
from cassini_viewer.transcription import TranscriptionQueue
from cassini_viewer.voice_store import VoiceStore


class Models:
    def request(self, directory, *, origin):
        return dict(modelPath="model", runtimePath="runtime", origin=origin)

    def measured(self, _factor):
        pass


def meeting(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("sound", "The others", "system", 16000, 1)
    session.append("sound", b"\0\0" * 80000, 0)
    session.append("sound", b"\0\0" * 80000, 5000)
    session.finish()
    return session.directory


@pytest.mark.parametrize("failure", [None, "model", "forget", "cancel", "manual", "clear", "cleared-before"])
def test_optional_voice_step_preserves_transcript_and_obeys_generation_and_cancel(tmp_path, failure):
    directory = meeting(tmp_path)
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    key = "Received-12345678-1234-4234-8234-123456789abc"
    store.commit_job(key, {"guest": dict(embedding=embedding(), seconds=8)}, 0)
    store.remember(key, "guest", "Alice")
    finished = []
    if failure == "cleared-before":
        update_metadata(
            directory, speakers=[dict(id="others", name=None, provenance="unnamed", colourIndex=0, nameCleared=True)]
        )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def cancel(self):
            pass

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            assert self.keep_for_voices
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=1,
                    words=[dict(text="Hello", startMs=0, endMs=9000)],
                )
            )
            return dict(diagnostics=[])

        def voiceprints(self, tracks, options, heartbeat):
            assert json.loads((directory / "transcript.json").read_text())["items"][0]["speaker"] == "others"
            assert options["directory"] == str(directory)
            if failure == "manual":
                update_metadata(
                    directory,
                    speakers=[dict(id="others", name="Typed while extracting", provenance="user", colourIndex=0)],
                )
            if failure == "clear":
                update_metadata(
                    directory,
                    speakers=[dict(id="others", name=None, provenance="unnamed", colourIndex=0, nameCleared=True)],
                )
            if failure == "model":
                raise RuntimeError("native model failed")
            if failure == "forget":
                store.forget_everyone()
            if failure == "cancel":
                queue.close()
                heartbeat()
                raise AssertionError("Cancellation was ignored")
            return {"others": dict(embedding=embedding(), seconds=8, windows=[dict(startMs=200, endMs=8200)])}

    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        bundle=tmp_path,
        root=tmp_path,
        client_factory=Client,
        diarization_models=Models(),
        voice_models=Models(),
        voice_store=store,
    )
    assert queue.enqueue(directory)
    until(lambda: bool(finished))
    metadata = read_metadata(directory)
    if failure == "cancel":
        assert metadata["transcript"]["status"] in ("queued", "running")
        assert not store.meeting(directory)["speakers"]
    else:
        assert metadata["transcript"]["status"] == "done"
        if failure is None:
            (speaker,) = metadata["speakers"]
            assert speaker["name"] == "Alice" and speaker["provenance"] == "recognised"
        elif failure == "manual":
            assert metadata["speakers"][0]["name"] == "Typed while extracting"
            assert metadata["speakers"][0]["provenance"] == "user"
            assert store.meeting(directory)["speakers"]["others"]["match"]["state"] == "suggested"
            assert not store.has_match(directory, "others")
            assert directory.name not in store.snapshot()["voices"][0]["meetings"]
        elif failure in ("clear", "cleared-before"):
            assert metadata["speakers"][0]["name"] is None and metadata["speakers"][0]["provenance"] == "unnamed"
            assert metadata["speakers"][0]["nameCleared"] is True
            assert store.meeting(directory)["speakers"]["others"]["match"]["state"] == "suggested"
            assert not store.has_match(directory, "others")
            assert directory.name not in store.snapshot()["voices"][0]["meetings"]
        else:
            assert metadata["speakers"][0]["name"] != "Alice"
    assert "embedding" not in json.dumps(metadata)
    assert "embedding" not in (directory / "transcript.json").read_text()
    queue.close()


@pytest.mark.parametrize("stop_at", [2, 4])
def test_store_check_active_after_staging_never_publishes_cancelled_print(tmp_path, stop_at):
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    calls = []

    def check():
        calls.append(True)
        if len(calls) == stop_at:
            raise CancelledError()

    with pytest.raises(CancelledError):
        store.commit_job(
            "Recording-12345678-1234-4234-8234-123456789abc",
            {"others": dict(embedding=embedding(), seconds=8)},
            0,
            check_active=check,
        )
    assert not list((store.root / "meetings").glob("*.json"))


def test_meeting_remains_readable_after_names_are_forgotten(tmp_path):
    directory = meeting(tmp_path)
    update_metadata(directory, speakers=[dict(id="others", name="Alice", provenance="recognised", colourIndex=0)])
    before = (directory / "meeting.json").read_bytes()
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    store.commit_job(directory, {"others": dict(embedding=embedding(), seconds=8)}, 0)
    store.remember(directory, "others", "Alice")
    identifier = store.snapshot()["voices"][0]["id"]
    assert store.has_match(directory, "others")
    store.forget(identifier)
    assert not store.has_match(directory, "others")
    assert (directory / "meeting.json").read_bytes() == before
    assert LibraryScanner(tmp_path).scan().meetings[0].metadata["speakers"][0]["name"] == "Alice"


def test_merge_and_undo_coalesce_to_latest_print_without_releasing_hold_early(tmp_path, monkeypatch):
    import threading

    from cassini_viewer import voice_models

    directory = meeting(tmp_path)
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    store.commit_job(directory, {"others": dict(embedding=embedding(), seconds=8)}, 0)
    entered, release = threading.Event(), threading.Event()
    captures = []

    class ReadyModels(Models):
        def snapshot(self):
            return dict(ready=True)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def voiceprints(self, tracks, options, heartbeat):
            captures.append(options["speakerMerges"])
            if len(captures) == 1:
                entered.set()
                assert release.wait(4)
            return {"others": dict(embedding=embedding(len(captures)), seconds=8)}

    monkeypatch.setattr(voice_models, "VoiceJobClient", Client)
    finished, failed = [], []
    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        bundle=tmp_path,
        root=tmp_path,
        diarization_models=ReadyModels(),
        voice_models=ReadyModels(),
        voice_store=store,
    )
    assert queue.recompute_voices(directory, {"others"}, failed.append)
    assert entered.wait(3)
    with queue.lock:
        assert queue.voice_pending == {directory}
        assert not queue.pending
    update_metadata(directory, speakerMerges=[dict(**{"from": "old"}, into="others")])
    assert not queue.recompute_voices(directory, {"old", "others"}, failed.append)
    assert not finished
    release.set()
    until(lambda: bool(finished))
    assert len(captures) == 2 and captures[0] == [] and captures[1] == [{"from": "old", "into": "others"}]
    assert finished == [directory] and not failed
    assert store.meeting(directory)["speakers"]["others"]["embedding"] == embedding(2)
    with queue.lock:
        assert not queue.voice_pending
    queue.close()


def test_request_at_completion_boundary_runs_instead_of_stranding_a_rerun(tmp_path, monkeypatch):
    import threading

    from cassini_viewer import voice_models

    directory = meeting(tmp_path)
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    captures, finished, failed, submitted = [], [], [], []
    armed = False

    class ReadyModels(Models):
        def snapshot(self):
            return dict(ready=True)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def close(self):
            nonlocal armed
            armed = len(captures) == 1

        def voiceprints(self, tracks, options, heartbeat):
            captures.append(True)
            return {"others": dict(embedding=embedding(len(captures)), seconds=8)}

    class CompletionLock:
        def __init__(self):
            self.inner = threading.RLock()
            self.fired = False

        def __enter__(self):
            self.inner.acquire()

        def __exit__(self, *_args):
            self.inner.release()
            if armed and not self.fired:
                self.fired = True
                submitted.append(queue.recompute_voices(directory, {"others"}, failed.append))

    monkeypatch.setattr(voice_models, "VoiceJobClient", Client)
    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        bundle=tmp_path,
        root=tmp_path,
        diarization_models=ReadyModels(),
        voice_models=ReadyModels(),
        voice_store=store,
    )
    queue.lock = CompletionLock()
    queue._idle = threading.Condition(queue.lock.inner)
    try:
        assert queue.recompute_voices(directory, {"others"}, failed.append)
        until(lambda: bool(finished))
        queue.executor.submit(lambda: None).result(timeout=4)
        assert submitted == [True] and len(captures) == 2
        assert finished == [directory, directory] and not failed
        assert not queue.voice_pending and not queue._voice_reruns
        assert store.meeting(directory)["speakers"]["others"]["embedding"] == embedding(2)
    finally:
        queue.close()
