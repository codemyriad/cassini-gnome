# SPDX-License-Identifier: GPL-3.0-or-later
"""Real fixture HTTP/store operations; injected capability probe, no native load."""

import hashlib
import threading
import time

import pytest
import test_model_download
from test_transcription import until

from cassini_talk import diarization_models, nemotron_model
from cassini_talk.diarization_models import DiarizationModels
from cassini_talk.model_download import ModelStore


class Probe:
    calls = []

    def __init__(self, bundle, model, runtime, **_kwargs):
        self.calls.append((bundle, model, runtime))
        self.ready = dict(model=nemotron_model.MODEL_TAG, sampleRate=16000, version="test.nemotron-diarization-v2")

    def start(self):
        return self

    def close(self):
        pass


@pytest.fixture
def remote():
    yield from test_model_download.remote.__wrapped__()


@pytest.fixture
def fixture(remote, tmp_path, monkeypatch):
    Probe.calls = []
    remote.manifest["models"][0].update(
        id=nemotron_model.MODEL_ID, source_sha256=nemotron_model.REVISION, license="OpenMDW-1.1"
    )
    pin = dict(
        size=len(remote.data),
        sha256=hashlib.sha256(remote.data).hexdigest(),
        downloadBytes=len(remote.compressed),
        artifactSHA256=hashlib.sha256(remote.compressed).hexdigest(),
    )
    monkeypatch.setattr(nemotron_model, "FILES", {"weights.onnx": (pin["size"], pin["sha256"], pin["downloadBytes"])})
    monkeypatch.setattr(nemotron_model, "PIN", dict(license="OpenMDW-1.1", files={"weights.onnx": pin}))
    monkeypatch.setattr(diarization_models, "verify_runtime", lambda path: path)
    store = ModelStore(tmp_path / "models", origin=remote.origin, fixture=True)
    models = DiarizationModels(tmp_path / "bundle", lambda: None, store=store, client_factory=Probe)
    until(lambda: not models.snapshot()["busy"])
    yield models, store, remote
    models.close()
    models._thread.join(2)
    assert not models._thread.is_alive()


def download(models):
    assert models.download()
    until(lambda: not models.snapshot()["busy"])


def test_explicit_pinned_download_runtime_gate_remove_and_notice(fixture):
    models, store, remote = fixture
    assert not remote.requests and not models.snapshot()["ready"]
    download(models)
    assert models.snapshot()["ready"] and models.snapshot()["downloaded"] and not models.snapshot()["error"]
    assert len(Probe.calls) == 1
    assert (store.root / nemotron_model.MODEL_ID / nemotron_model.REVISION / ".cassini-NOTICE.txt").is_file()
    options = models.request(store.root, origin="received")
    assert options["origin"] == "received" and options["realtimeFactor"] == 0.05
    models.measured(0.012)
    assert models.request(store.root, origin="own")["realtimeFactor"] == 0.012
    assert models.remove() and not models.snapshot()["ready"]
    until(lambda: not models.snapshot()["busy"])
    assert not store.installed(nemotron_model.MODEL_ID, nemotron_model.REVISION)


@pytest.mark.parametrize("failure", ["construct", "start", "unsupported"])
def test_runtime_failure_keeps_verified_weights_and_retry_recovers(fixture, failure):
    models, store, _remote = fixture

    class Broken(Probe):
        def __init__(self, *args, **kwargs):
            if failure == "construct":
                raise OSError("Runtime unavailable")
            super().__init__(*args, **kwargs)
            if failure == "unsupported":
                self.ready["version"] = "unrelated fork"

        def start(self):
            if failure == "start":
                raise RuntimeError("Native initialization failed")
            return self

    models.client_factory = Broken
    download(models)
    state = models.snapshot()
    assert state["downloaded"] and not state["ready"] and state["error"]
    assert store.installed(nemotron_model.MODEL_ID, nemotron_model.REVISION)
    models.client_factory = Probe
    assert models.retry()
    until(lambda: not models.snapshot()["busy"])
    assert models.snapshot()["ready"] and not models.snapshot()["error"]


def test_damaged_weights_never_reach_worker_and_invalidate_without_nested_lock(fixture):
    models, store, _remote = fixture
    download(models)
    count = len(Probe.calls)
    model = store.root / nemotron_model.MODEL_ID / nemotron_model.REVISION
    (model / "weights.onnx").write_bytes(b"damaged")
    assert models.request(store.root, origin="own") is None
    assert len(Probe.calls) == count and not model.exists()
    assert not models.snapshot()["downloaded"] and not models.snapshot()["ready"]


@pytest.mark.parametrize("field", ["license", "compressed"])
def test_changed_dist_licence_or_artifact_is_rejected_before_weight_fetch(fixture, field):
    models, store, remote = fixture
    if field == "license":
        remote.manifest["models"][0]["license"] = "CC-BY-4.0"
    else:
        remote.manifest["artifacts"][0]["sha256"] = "0" * 64
    download(models)
    assert models.snapshot()["error"] and not models.snapshot()["ready"]
    assert all(path == "/manifest.json" for path, _headers in remote.requests)
    assert not store.installed(nemotron_model.MODEL_ID, nemotron_model.REVISION)


def test_cancel_after_publish_keeps_the_verified_model_on(fixture, monkeypatch):
    models, store, _remote = fixture
    original = store.download

    def publish(*args, **kwargs):
        path = original(*args, **kwargs)
        kwargs["cancelled"].set()
        return path

    monkeypatch.setattr(store, "download", publish)
    download(models)
    assert models.snapshot()["downloaded"] and models.snapshot()["ready"]


def test_close_signals_a_busy_capability_child_without_joining_gtk(fixture):
    models, _store, _remote = fixture
    entered, release = threading.Event(), threading.Event()

    class Busy(Probe):
        def start(self):
            entered.set()
            assert release.wait(3)
            from concurrent.futures import CancelledError

            raise CancelledError()

        def cancel(self):
            release.set()

    models.client_factory = Busy
    assert models.download() and entered.wait(2)
    started = time.monotonic()
    models.close()
    assert time.monotonic() - started < 0.1
    until(lambda: not models.snapshot()["busy"])
