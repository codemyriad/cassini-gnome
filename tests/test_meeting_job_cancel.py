# SPDX-License-Identifier: GPL-3.0-or-later
"""D29 cancels and waits for one meeting without shutting down the shared queue."""

import threading
from concurrent.futures import ThreadPoolExecutor

from durable import DURABLE_BOUND, Changes, Finished
from test_library import capture
from test_voice_queue import Models, meeting
from test_voice_store import embedding
from test_whistle_spike import child_client

from cassini_talk.meeting_store import read_metadata
from cassini_talk.transcription import TranscriptionQueue
from cassini_talk.voice_store import VoiceStore


def test_cancel_queued_and_running_transcription_keeps_other_meeting(tmp_path, monkeypatch):
    sessions = [capture(tmp_path) for _ in range(3)]
    children, finished, changes = [], Finished(), Changes()

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
            """import json,signal,sys
print(json.dumps(dict(type='ready')),flush=True)
request=json.loads(sys.stdin.readline())
track=request['tracks'][0]
print(json.dumps(dict(type='progress',trackId=track['id'],trackOrder=0,language='en',etaMinutes=1,
words=[dict(text='Partial',startMs=100,endMs=200)])),flush=True)
signal.pause()
""",
            timeout=2 * DURABLE_BOUND,
        )
        children.append(client)
        return client

    queue = TranscriptionQueue(changes, finished, client_factory=factory, root=tmp_path, bundle=tmp_path)
    worker = ThreadPoolExecutor(1)
    try:
        for session in sessions:
            assert queue.enqueue(session.directory)
        first, queued, other = [s.directory for s in sessions]
        changes.wait(
            lambda: (first / "transcript.json").exists() and b"Partial" in (first / "transcript.json").read_bytes(),
            "the first job did not publish its partial words",
        )
        changes.wait(
            lambda: read_metadata(queued)["transcript"]["status"] == "queued",
            "the queued meeting did not publish its intent",
        )
        client = children[0]
        child = client.process
        assert child is not None and child.poll() is None

        def remove_queued():
            with queue.cancel_meeting(queued):
                assert not queue.enqueue(queued)
                queued.rename(tmp_path / "private-trash-queued")

        def remove_active():
            with queue.cancel_meeting(first):
                assert child.poll() is not None, "Trash exclusion returned before the child exited"
                assert read_metadata(first)["transcript"]["status"] == "running", (
                    "cancellation marked partial words failed"
                )
                first.rename(tmp_path / "private-trash-active")

        worker.submit(remove_queued).result(timeout=DURABLE_BOUND)
        assert not client.cancelled.is_set(), "cancelling a queued meeting signalled the unrelated active child"
        assert child.poll() is None, "cancelling a queued meeting killed the unrelated active child"
        assert first in queue.pending and other in queue.pending and queued not in queue.pending
        worker.submit(remove_active).result(timeout=DURABLE_BOUND)
        finished.wait(3)  # The two cancelled jobs and the other one.
        assert read_metadata(other)["transcript"]["status"] == "done"
        assert not first.exists() and not queued.exists(), "a late job recreated a removed folder"
        assert sorted(finished) == sorted([first, queued, other])
        assert not queue.closed
    finally:
        queue.close()
        worker.shutdown(wait=False, cancel_futures=True)


def test_cancel_running_voice_job_keeps_next_meeting_and_no_private_print(tmp_path, monkeypatch):
    from cassini_talk import voice_models

    directories = [meeting(tmp_path) for _ in range(2)]
    entered, release = threading.Event(), threading.Event()
    finished, failures, clients = Finished(), [], []
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
        finished,
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
        finished.wait(2, exactly=True)
        assert not failures and not directories[0].exists()
        assert not (store.root / "meetings" / (directories[0].name + ".json")).exists()
        assert store.meeting(directories[1])["speakers"]["others"]["embedding"] == embedding()
        assert not queue.closed
    finally:
        release.set()
        queue.close()
