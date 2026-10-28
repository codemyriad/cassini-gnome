# SPDX-License-Identifier: GPL-3.0-or-later
"""Metadata publication and completed-recognition salvage after child failure."""

import hashlib
import json

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
