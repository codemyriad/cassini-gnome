# SPDX-License-Identifier: GPL-3.0-or-later
"""D29 cancels and waits for one meeting without shutting down the shared queue."""

import threading
from concurrent.futures import ThreadPoolExecutor

from test_library import capture
from test_transcription import until
from test_voice_queue import Models, meeting
from test_voice_store import embedding
from test_whistle_spike import child_client

from cassini_talk.meeting_store import read_metadata
from cassini_talk.transcription import TranscriptionQueue
from cassini_talk.voice_store import VoiceStore


def test_cancel_queued_and_running_transcription_keeps_other_meeting(tmp_path, monkeypatch):
    sessions = [capture(tmp_path) for _ in range(3)]
    children, finished = [], []

    class FastClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def process_tracks(self, _tracks, _notify):
            return dict(diagnostics=[])

    def factory(_bundle, **_kwargs):
        if children:
            return FastClient()
        client = child_client(
            tmp_path,
            monkeypatch,
            """import json,sys,time
print(json.dumps(dict(type='ready')),flush=True)
request=json.loads(sys.stdin.readline())
track=request['tracks'][0]
print(json.dumps(dict(type='progress',trackId=track['id'],trackOrder=0,language='en',etaMinutes=1,
words=[dict(text='Partial',startMs=100,endMs=200)])),flush=True)
time.sleep(30)
""",
        )
        children.append(client)
        return client

    queue = TranscriptionQueue(lambda _: None, finished.append, client_factory=factory, root=tmp_path, bundle=tmp_path)
    try:
        for session in sessions:
            assert queue.enqueue(session.directory)
        first, queued, other = [s.directory for s in sessions]
        until(lambda: (first / "transcript.json").exists() and b"Partial" in (first / "transcript.json").read_bytes())
        until(lambda: read_metadata(queued)["transcript"]["status"] == "queued")
        child = children[0].process
        with ThreadPoolExecutor(1) as worker:

            def remove_queued():
                with queue.cancel_meeting(queued):
                    assert not queue.enqueue(queued)
                    queued.rename(tmp_path / "private-trash-queued")

            worker.submit(remove_queued).result(timeout=2)
        assert child.poll() is None, "cancelling a queued meeting killed the unrelated active child"
        assert first in queue.pending and other in queue.pending and queued not in queue.pending
        with queue.cancel_meeting(first):
            assert child.poll() is not None, "Trash exclusion returned before the child exited"
            assert read_metadata(first)["transcript"]["status"] == "running", "cancellation marked partial words failed"
            first.rename(tmp_path / "private-trash-active")
        until(lambda: other in finished)
        assert read_metadata(other)["transcript"]["status"] == "done"
        assert not first.exists() and not queued.exists(), "a late job recreated a removed folder"
        assert sorted(finished) == sorted([first, queued, other])
        assert not queue.closed
    finally:
        queue.close()


def test_cancel_running_voice_job_keeps_next_meeting_and_no_private_print(tmp_path, monkeypatch):
    from cassini_talk import voice_models

    directories = [meeting(tmp_path) for _ in range(2)]
    entered, release = threading.Event(), threading.Event()
    finished, failures, clients = [], [], []
    store = VoiceStore(tmp_path / "private")
    store.refresh()

    class ReadyModels(Models):
        def snapshot(self):
            return dict(ready=True)

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.first = not clients
            clients.append(self)

        def start(self):
            pass

        def close(self):
            pass

        def cancel(self):
            release.set()

        def voiceprints(self, _tracks, _options, heartbeat):
            if self.first:
                entered.set()
                assert release.wait(3), "per-meeting cancellation did not reach the active voice client"
                heartbeat()
                raise AssertionError("cancelled voice job continued")
            return {"others": dict(embedding=embedding(), seconds=8)}

    monkeypatch.setattr(voice_models, "VoiceJobClient", Client)
    queue = TranscriptionQueue(
        lambda _: None,
        finished.append,
        root=tmp_path,
        bundle=tmp_path,
        voice_models=ReadyModels(),
        diarization_models=ReadyModels(),
        voice_store=store,
    )
    try:
        for directory in directories:
            assert queue.recompute_voices(directory, {"others"}, failures.append)
        assert entered.wait(2)
        with queue.cancel_meeting(directories[0]):
            assert directories[0] not in queue.voice_pending
            directories[0].rename(tmp_path / "private-trash-voice")
        until(lambda: len(finished) == 2)
        assert not failures and not directories[0].exists()
        assert not (store.root / "meetings" / (directories[0].name + ".json")).exists()
        assert store.meeting(directories[1])["speakers"]["others"]["embedding"] == embedding()
        assert not queue.closed
    finally:
        release.set()
        queue.close()
