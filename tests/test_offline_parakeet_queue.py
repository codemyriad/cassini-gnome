# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the offline probe's actual adapter through the production FIFO (E1)."""

import importlib.util
import json
from pathlib import Path

from durable import Finished
from test_library import capture

from cassini_talk.meeting_store import read_metadata
from cassini_talk.parakeet_model import MODEL_TAG
from cassini_talk.transcription import TranscriptionQueue


def test_offline_probe_adapter_completes_both_fifo_jobs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "offline_queue_models", Path(__file__).with_name("parakeet") / "queue_models.py"
    )
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    sessions = [capture(tmp_path), capture(tmp_path)]
    calls = []

    class Client:
        def __init__(self, bundle, model, runtime, *, timeout):
            assert (bundle, model, runtime, timeout) == (tmp_path, tmp_path / "model", tmp_path / "runtime", 120)
            self.process = None

        def start(self):
            assert all(client.process is None for client in calls)
            self.process = object()
            calls.append(self)

        def process_tracks(self, tracks, notify):
            notify(dict(type="planned", etaMinutes=0))
            notify(
                dict(
                    type="progress",
                    etaMinutes=0,
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    words=[dict(startMs=0, endMs=10, text="Hello")],
                )
            )
            return dict(diagnostics=[])

        def close(self):
            self.process = None

    def no_whistle(*_args, **_kwargs):
        raise AssertionError("The offline Parakeet probe fell back to Whistle")

    models = adapter.Models(tmp_path / "model", tmp_path / "runtime", client_factory=Client)
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        root=tmp_path,
        bundle=tmp_path,
        models=models,
        client_factory=no_whistle,
    )
    try:
        assert all(queue.enqueue(session.directory) for session in sessions)
        finished.wait(2, exactly=True)
        for session in sessions:
            assert read_metadata(session.directory)["transcript"]["status"] == "done"
            assert read_metadata(session.directory)["transcript"]["model"] == MODEL_TAG
            assert json.loads((session.directory / "transcript.json").read_text())["items"][0]["text"] == "Hello"
        assert len(models.clients) == len(models.children) == 2
        assert all(client.process is None for client in models.clients)
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
