# SPDX-License-Identifier: GPL-3.0-or-later
"""Real fixture HTTP/store operations; injected capability probe, no native load."""

import hashlib
import threading
import time

import pytest
import test_model_download
from durable import DURABLE_BOUND, Changes, idle

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
    store = ModelStore(
        tmp_path / "models", origin=remote.origin, fixture=True, user_agent="CassiniTalk/test (GNOME; Linux; source)"
    )
    models = DiarizationModels(tmp_path / "bundle", lambda: None, store=store, client_factory=Probe)
    idle(models)
    yield models, store, remote
    models.close()
    idle(models)


def download(models):
    assert models.download()
    idle(models)  # The download installs the model durably (14 fsyncs).


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
    idle(models)
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
    idle(models)
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
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    timed_out, closed = threading.Event(), threading.Event()
    changes, closed_threads = Changes(), []
    models.changed = changes

    class Busy(Probe):
        def start(self):
            entered.set()
            changes()
            # Hang guard: a joining-close regression must fail this test rather
            # than deadlock the whole shard. Healthy execution releases by event.
            if not release.wait(DURABLE_BOUND):
                timed_out.set()
            if cancelled.is_set():
                from concurrent.futures import CancelledError

                raise CancelledError()
            return self

        def cancel(self):
            cancelled.set()

        def close(self):
            closed_threads.append(threading.get_ident())
            closed.set()

    models.client_factory = Busy
    assert models.download()
    worker = models._thread
    try:
        changes.wait(
            lambda: entered.is_set() or not models.snapshot()["busy"],
            "the download did not reach the runtime start",
        )
        assert entered.is_set(), f"Worker exited before capability start: {models.snapshot()}"
        started_cpu = time.thread_time_ns()
        models.close()
        # Preserve the 100 ms CPU ceiling; the event protocol separately detects
        # joins/lock waits. The durable guard only lets a broken join unwind.
        assert time.thread_time_ns() - started_cpu < 100_000_000
        assert not timed_out.is_set(), "Close waited for the held capability worker"
        assert cancelled.is_set(), "Close did not signal the active capability child"
        assert models.cancelled.is_set() and models.download_cancelled.is_set()
        assert not release.is_set() and worker.is_alive(), "Close must not wait for worker completion"
        assert not closed.is_set(), "The capability child was reaped on the calling thread"
    finally:
        release.set()
        idle(models)
    assert closed.is_set() and closed_threads == [worker.ident]
    assert models._probe is None
    state = models.snapshot()
    assert state["downloaded"] and not state["ready"] and not state["error"]
