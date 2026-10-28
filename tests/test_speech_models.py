# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit D4 acquisition and D16 job-start selection, using loopback weights."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import test_model_download
from test_library import capture
from test_transcription import until

from cassini_viewer import parakeet_model
from cassini_viewer.meeting_store import read_metadata
from cassini_viewer.model_download import ModelStore
from cassini_viewer.parakeet_model import MODEL_ID, MODEL_TAG, REVISION
from cassini_viewer.speech_models import SpeechModels
from cassini_viewer.transcription import TranscriptionQueue


@pytest.fixture
def remote():
    # Explicit fixture ownership also works when pytest collected the source
    # module before this module; late plugin registration is version-dependent.
    yield from test_model_download.remote.__wrapped__()


@pytest.fixture
def model_fixture(remote, tmp_path, monkeypatch):
    remote.manifest["models"][0].update(id=MODEL_ID, source_sha256=REVISION)
    monkeypatch.setattr(
        parakeet_model,
        "FILES",
        {"weights.onnx": (len(remote.data), hashlib.sha256(remote.data).hexdigest(), len(remote.compressed))},
    )
    store = ModelStore(tmp_path / "models", origin=remote.origin, fixture=True)
    persisted, notifications = [], []
    models = SpeechModels("whistle", persisted.append, lambda: notifications.append(True), store=store)
    until(lambda: not models.snapshot()["busy"])
    yield models, store, remote, persisted
    models.close()
    models._thread.join(2)
    assert not models._thread.is_alive()


def downloaded(models):
    assert models.download()
    until(lambda: not models.snapshot()["busy"])
    assert models.snapshot()["downloaded"] and not models.snapshot()["error"]


def test_download_is_explicit_pinned_and_never_selects_itself(model_fixture):
    models, store, remote, persisted = model_fixture
    assert not remote.requests and not models.select(MODEL_ID)
    downloaded(models)
    assert models.snapshot()["selected"] == "whistle" and not persisted
    assert (store.root / MODEL_ID / REVISION / "weights.onnx").read_bytes() == remote.data
    assert models.select(MODEL_ID) and persisted[-1] == MODEL_ID
    assert models.remove() and persisted[-1] == "whistle"
    assert models.snapshot()["selected"] == "whistle"
    until(lambda: not models.snapshot()["busy"])
    assert not (store.root / MODEL_ID / REVISION).exists()
    assert models.snapshot()["notice"] == "models.parakeetUltra.removedFallback"


def test_changed_manifest_is_rejected_before_any_weight_request(model_fixture):
    models, store, remote, _ = model_fixture
    remote.manifest["artifacts"][0]["uncompressed_sha256"] = "0" * 64
    assert models.download()
    until(lambda: not models.snapshot()["busy"])
    assert models.snapshot()["error"] and not models.snapshot()["downloaded"]
    assert [path for path, _ in remote.requests] == ["/manifest.json"]
    assert not (store.root / MODEL_ID / REVISION).exists()


def test_corrupt_weight_resets_choice_deletes_revision_and_falls_back_before_loading(model_fixture):
    models, store, _, persisted = model_fixture
    downloaded(models)
    models.select(MODEL_ID)
    (store.root / MODEL_ID / REVISION / "weights.onnx").write_bytes(b"corrupted")
    models.client_factory = lambda *_a, **_k: pytest.fail("Bad weights reached native engine")
    assert models.start_parakeet(Path("unused"), register=lambda _: pytest.fail("Registered bad engine")) is None
    assert persisted[-1] == "whistle" and models.snapshot()["notice"] == "models.parakeetUltra.invalidFallback"
    assert not (store.root / MODEL_ID / REVISION).exists()


def test_valid_weights_survive_a_runtime_start_failure(model_fixture):
    models, store, _, _ = model_fixture
    downloaded(models)
    models.select(MODEL_ID)

    class BrokenRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("Pinned runtime missing")

    models.client_factory = BrokenRuntime
    with pytest.raises(RuntimeError, match="runtime missing"):
        models.start_parakeet(Path("unused"), register=lambda _: None)
    assert (store.root / MODEL_ID / REVISION / "weights.onnx").is_file()


def test_initial_verification_is_offline_and_invalid_selection_is_reset(model_fixture):
    models, store, remote, _ = model_fixture
    downloaded(models)
    models.close()
    (store.root / MODEL_ID / REVISION / "weights.onnx").write_bytes(b"bad")
    before, persisted = len(remote.requests), []
    second = SpeechModels(MODEL_ID, persisted.append, lambda: None, store=store)
    try:
        until(lambda: not second.snapshot()["busy"])
        assert persisted == ["whistle"] and not second.snapshot()["downloaded"]
        assert len(remote.requests) == before and not (store.root / MODEL_ID / REVISION).exists()
    finally:
        second.close()


@pytest.mark.parametrize("first_model", ["whistle", MODEL_ID])
def test_fifo_reads_model_when_job_starts_and_remove_keeps_loaded_job(
    model_fixture, tmp_path, monkeypatch, first_model
):
    models, store, _, persisted = model_fixture
    downloaded(models)
    models.select(first_model)
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    sessions = [capture(root), capture(root)]
    entered, release = threading.Event(), threading.Event()
    calls, finished, active = [], [], set()

    class Client:
        kind = "whistle"

        def __init__(self, *_args, **_kwargs):
            self.loaded = False

        def start(self):
            assert not active, "Two speech workers overlap"
            active.add(self)
            self.loaded = True

        def close(self):
            active.discard(self)

        def process_tracks(self, tracks, notify):
            assert self.loaded
            calls.append(self.kind)
            if len(calls) == 1:
                entered.set()
                assert release.wait(3)
            notify(dict(type="planned", etaMinutes=0))
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="und",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=10, text="written")],
                )
            )
            return dict(diagnostics=[])

    class Parakeet(Client):
        kind = MODEL_ID

        def start(self):
            assert (store.root / MODEL_ID / REVISION / "weights.onnx").exists()
            super().start()

    models.client_factory = Parakeet
    queue = TranscriptionQueue(lambda _: None, finished.append, bundle=tmp_path, client_factory=Client, models=models)
    try:
        assert queue.enqueue(sessions[0].directory)
        until(entered.is_set)
        assert queue.enqueue(sessions[1].directory)
        until(lambda: (sessions[1].directory / "meeting.json").exists())
        if first_model == MODEL_ID:
            assert models.remove() and persisted[-1] == "whistle"
            until(lambda: not models.snapshot()["busy"])
            assert not (store.root / MODEL_ID / REVISION).exists()
            expected = [MODEL_ID, "whistle"]
        else:
            models.select(MODEL_ID)
            expected = ["whistle", MODEL_ID]
        release.set()
        until(lambda: len(finished) == 2)
        assert calls == expected
        for session, kind in zip(sessions, expected, strict=True):
            metadata = read_metadata(session.directory)
            assert metadata["transcript"] == dict(
                status="done",
                reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1"),
                model=MODEL_TAG if kind == MODEL_ID else "whistle",
            )
            assert json.loads((session.directory / "transcript.json").read_text())["items"][0]["text"] == "written"
    finally:
        release.set()
        queue.close()
        until(lambda: not active)


def test_quit_during_stalled_download_does_not_wait_for_network(tmp_path):
    # A real interpreter must exit before the artificial network read returns;
    # a ThreadPoolExecutor here would make this take ten seconds.
    script = """
import threading, time
from cassini_viewer.speech_models import SpeechModels
class Store:
    def _directory(self, *args):
        from pathlib import Path
        return Path("/nonexistent-cassini-model")
    def fetch_manifest(self, **kwargs):
        entered.set()
        time.sleep(10)
entered = threading.Event()
m = SpeechModels("whistle", lambda _:None, lambda:None, store=Store())
while m.snapshot()["busy"]: time.sleep(.001)
m.download()
assert entered.wait(1)
m.close()
"""
    started = time.monotonic()
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        timeout=2,
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
    )
    assert time.monotonic() - started < 0.8


def test_remove_during_native_load_persists_fallback_without_deleting_loading_files(model_fixture):
    models, store, _, persisted = model_fixture
    downloaded(models)
    models.select(MODEL_ID)
    entered, release = threading.Event(), threading.Event()
    loaded = []
    path = store.root / MODEL_ID / REVISION / "weights.onnx"

    class LoadingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            entered.set()
            assert release.wait(3)
            assert path.is_file()
            loaded.append(path.read_bytes())

    models.client_factory = LoadingClient
    owner = threading.Thread(target=lambda: models.start_parakeet(Path("unused"), register=lambda _: None))
    owner.start()
    try:
        assert entered.wait(2)
        assert models.remove() and persisted[-1] == "whistle"
        assert path.is_file(), "Removal raced the in-progress native model load"
        release.set()
        owner.join(2)
        until(lambda: not models.snapshot()["busy"])
        assert loaded and not path.exists()
    finally:
        release.set()
        owner.join(2)


def test_cancel_explicit_download_keeps_controller_retryable(model_fixture, monkeypatch):
    models, _, _, _ = model_fixture
    entered = threading.Event()
    original = models.store.fetch_manifest

    def fetch(*, cancelled):
        entered.set()
        assert cancelled.wait(2)
        from concurrent.futures import CancelledError

        raise CancelledError()

    monkeypatch.setattr(models.store, "fetch_manifest", fetch)
    assert models.download() and entered.wait(1)
    models.cancel_download()
    until(lambda: not models.snapshot()["busy"])
    assert not models.snapshot()["error"] and models.snapshot()["selected"] == "whistle"
    monkeypatch.setattr(models.store, "fetch_manifest", original)
    downloaded(models)
