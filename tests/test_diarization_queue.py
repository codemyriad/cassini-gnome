# SPDX-License-Identifier: GPL-3.0-or-later
"""Metadata publication and completed-recognition salvage after child failure."""

import hashlib
import json
import threading

import pytest
from test_transcription import until

from cassini_viewer.library import LibraryScanner
from cassini_viewer.meeting_store import read_metadata, update_metadata
from cassini_viewer.nemotron_model import MODEL_TAG
from cassini_viewer.recording_session import RecordingSession
from cassini_viewer.transcription import TranscriptionQueue
from cassini_viewer.worker_diarization import FORMAT, PARAMS


class Models:
    def request(self, directory, *, origin):
        return dict(modelPath="test-model", runtimePath="test-runtime", origin=origin)

    def measured(self, _factor):
        pass


@pytest.mark.parametrize("failure", [None, "native-crash", "model-error"])
def test_voice_publication_and_diarization_failures_preserve_words_and_capture(tmp_path, failure):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.iterdir()}
    update_metadata(
        session.directory,
        speakers=[dict(id="others~1", name="Alice", provenance="user", colourIndex=0)],
        notes=[dict(atMs=100, text="Keep this")],
        privateField="preserved",
    )
    words = [
        dict(text="One", startMs=10, endMs=800, lowConfidenceSpeaker=True),
        dict(text="Two", startMs=1010, endMs=1800),
    ]
    turns = [dict(startMs=0, endMs=900, speaker=0), dict(startMs=1000, endMs=2000, speaker=1)]
    finished = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            assert self.diarization["modelPath"] == "test-model"
            notify(
                dict(type="progress", trackId=tracks[0]["id"], trackOrder=0, language="en", etaMinutes=1, words=words)
            )
            notify(dict(type="recognised", summary=dict(language="en", diagnostics=[]), etaMinutes=1))
            if failure == "native-crash":
                raise RuntimeError("Worker exited in native diarization")
            if failure == "model-error":
                return dict(diagnostics=[dict(code="diarization-kept-d11")])
            notify(
                dict(
                    type="diarized",
                    groupId="others",
                    durationMs=2000,
                    inputSHA256="a" * 64,
                    turns=turns,
                    first=True,
                    final=True,
                    etaMinutes=0,
                )
            )
            return dict(
                diagnostics=[],
                diarization=dict(
                    model=MODEL_TAG, params=PARAMS, groupIds=["others"], realtimeFactor=0.01, diagnostics=[]
                ),
            )

    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        bundle=tmp_path,
        root=tmp_path,
        client_factory=Client,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        until(lambda: len(finished) == 1)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "done" and metadata["privateField"] == "preserved"
        assert metadata["notes"] == [dict(atMs=100, text="Keep this")]
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert [{k: v for k, v in word.items() if k != "speaker"} for word in body["items"]] == words
        assert before == {name: hashlib.sha256((session.directory / name).read_bytes()).hexdigest() for name in before}
        if failure is None:
            assert [w["speaker"] for w in body["items"]] == ["others~1", "others~2"]
            cache = json.loads((session.directory / "speaker-turns.json").read_bytes())
            assert cache["format"] == FORMAT and cache["model"] == MODEL_TAG and cache["groups"][0]["turns"] == turns
            (meeting,) = LibraryScanner(tmp_path).scan().meetings
            assert meeting.speaker_names == ("Alice", "Speaker 2")
            assert meeting.speakers[0].provenance == "user"
        else:
            assert [w["speaker"] for w in body["items"]] == ["others", "others"]
            assert not (session.directory / "speaker-turns.json").exists()
    finally:
        queue.close()


def test_actual_child_crash_after_recognition_finishes_with_d11(tmp_path, monkeypatch):
    from test_whistle_spike import child_client

    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    client = child_client(
        tmp_path,
        monkeypatch,
        """import json,os,sys
print(json.dumps(dict(type='ready')))
request=json.loads(sys.stdin.readline())
track=request['tracks'][0]
print(json.dumps(dict(type='progress',trackId=track['id'],trackOrder=0,
    language='en',etaMinutes=1,words=[dict(text='Kept',startMs=10,endMs=800)])))
print(json.dumps(dict(type='recognised',summary=dict(language='en',diagnostics=[]),etaMinutes=1)))
os._exit(77)
""",
    )
    finished = []
    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=lambda *_args, **_kwargs: client,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        until(lambda: len(finished) == 1)
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert body["items"] == [dict(text="Kept", startMs=10, endMs=800, speaker="others")]
        assert not (session.directory / "speaker-turns.json").exists()
        assert client.process is None and queue.client is None
    finally:
        queue.close()


class CompletedClient:
    """Deterministic child messages, with one unpublished buffered word."""

    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        return self

    def close(self):
        pass

    def before_return(self):
        pass

    def process_tracks(self, tracks, notify):
        for text, start in (("One", 10), ("Two", 1010)):
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=1,
                    words=[dict(text=text, startMs=start, endMs=start + 700)],
                )
            )
        notify(dict(type="recognised", summary=dict(language="en", diagnostics=[]), etaMinutes=1))
        notify(
            dict(
                type="diarized",
                groupId="others",
                durationMs=2000,
                inputSHA256="a" * 64,
                turns=[dict(startMs=0, endMs=900, speaker=0), dict(startMs=1000, endMs=2000, speaker=1)],
                first=True,
                final=True,
                etaMinutes=0,
            )
        )
        self.before_return()
        return dict(
            diagnostics=[], diarization=dict(model=MODEL_TAG, params=PARAMS, groupIds=["others"], diagnostics=[])
        )


@pytest.fixture
def system_capture(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    return session


def file_snapshot(directory):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}


def test_close_after_worker_return_does_not_flush_buffered_words(system_capture, tmp_path):
    session = system_capture
    snapshots, finished = [], []

    class Client(CompletedClient):
        def before_return(self):
            queue.close()
            snapshots.append(file_snapshot(session.directory))

    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=Client,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        until(lambda: len(finished) == 1)
        assert len(snapshots) == 1
        assert file_snapshot(session.directory) == snapshots[0], "Queue committed files after close"
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert [w["text"] for w in body["items"]] == ["One"]
        assert read_metadata(session.directory)["transcript"]["status"] == "running"
        assert not queue.pending
    finally:
        queue.close()


def test_cancelled_attribution_remains_running_and_restart_redoes_missing_turns(system_capture, tmp_path, monkeypatch):
    from cassini_viewer import diarization, transcript_publish

    session = system_capture
    update_metadata(session.directory, speakers=[dict(id="others~1", name="Alice", provenance="user", colourIndex=0)])
    entered, release = threading.Event(), threading.Event()
    completed_flushes, finished = [], []
    apply = diarization.apply_transcript
    flush = transcript_publish.TranscriptPublisher.flush

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return apply(*args)

    def observed(publisher, **kwargs):
        if kwargs.get("complete"):
            completed_flushes.append(1)
        return flush(publisher, **kwargs)

    monkeypatch.setattr(diarization, "apply_transcript", paused)
    monkeypatch.setattr(transcript_publish.TranscriptPublisher, "flush", observed)
    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=CompletedClient,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        until(entered.is_set)
        assert not completed_flushes, "Final flush happened before speaker assignment"
        assert read_metadata(session.directory)["transcript"]["status"] == "running"
        assert not (session.directory / "speaker-turns.json").exists()
        queue.close()
        before = file_snapshot(session.directory)
        release.set()
        until(lambda: len(finished) == 1)
        assert file_snapshot(session.directory) == before
        assert not completed_flushes
    finally:
        release.set()
        queue.close()
    monkeypatch.setattr(diarization, "apply_transcript", apply)
    resumed = TranscriptionQueue(
        lambda _: None,
        finished.append,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=CompletedClient,
        diarization_models=Models(),
    )
    try:
        assert resumed.enqueue(session.directory, resume=True)
        until(lambda: len(finished) == 2)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "done"
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert [w["speaker"] for w in body["items"]] == ["others~1", "others~2"]
        assert json.loads((session.directory / "speaker-turns.json").read_bytes())["groups"]
        assert next(s for s in metadata["speakers"] if s["id"] == "others~1")["name"] == "Alice"
        assert len(completed_flushes) == 1
    finally:
        resumed.close()
