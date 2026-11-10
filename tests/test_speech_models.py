# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit D4 acquisition and D16 job-start selection, using loopback weights."""

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import test_model_download
from durable import Finished, durable, idle
from test_library import capture
from test_transcription import until

from cassini_talk import parakeet_model
from cassini_talk.meeting_store import read_metadata
from cassini_talk.model_download import ModelStore
from cassini_talk.parakeet_model import MODEL_ID, MODEL_TAG, REVISION
from cassini_talk.speech_models import SpeechModels
from cassini_talk.transcription import TranscriptionQueue


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
    store = ModelStore(
        tmp_path / "models", origin=remote.origin, fixture=True, user_agent="CassiniTalk/test (GNOME; Linux; source)"
    )
    persisted, notifications = [], []
    models = SpeechModels("whistle", persisted.append, lambda: notifications.append(True), store=store)
    idle(models)
    yield models, store, remote, persisted
    models.close()
    models._thread.join(2)
    assert not models._thread.is_alive()


def downloaded(models):
    assert models.download()
    idle(models)
    assert models.snapshot()["downloaded"] and not models.snapshot()["error"]


def test_explicit_download_verifies_then_selects_parakeet_default(model_fixture):
    models, store, remote, persisted = model_fixture
    assert not remote.requests and not models.select(MODEL_ID)
    downloaded(models)
    assert models.snapshot()["selected"] == MODEL_ID and persisted == [MODEL_ID]
    assert models.snapshot()["notice"] == "models.parakeetUltra.nowDefault"
    assert (store.root / MODEL_ID / REVISION / "weights.onnx").read_bytes() == remote.data
    assert models.select(MODEL_ID) and persisted[-1] == MODEL_ID
    assert models.remove() and persisted[-1] == "whistle"
    assert models.snapshot()["selected"] == "whistle"
    idle(models)
    assert not (store.root / MODEL_ID / REVISION).exists()
    assert models.snapshot()["notice"] == "models.parakeetUltra.removedFallback"


def test_changed_manifest_is_rejected_before_any_weight_request(model_fixture):
    models, store, remote, _ = model_fixture
    remote.manifest["artifacts"][0]["uncompressed_sha256"] = "0" * 64
    assert models.download()
    idle(models)
    assert models.snapshot()["error"] and not models.snapshot()["downloaded"]
    assert models.snapshot()["selected"] == "whistle"
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
        idle(second)
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
    calls, finished, active = [], Finished(), set()
    committed_models = []

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
                # Held while the test queues the second job and removes or
                # selects a model, durably, before release.set().
                durable(release, "the test did not release the first job")
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
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=tmp_path,
        client_factory=Client,
        models=models,
        completed=committed_models.append,
    )
    try:
        assert queue.enqueue(sessions[0].directory)
        durable(entered, "the first job did not start")
        assert queue.enqueue(sessions[1].directory)
        until(lambda: (sessions[1].directory / "meeting.json").exists())
        if first_model == MODEL_ID:
            assert models.remove() and persisted[-1] == "whistle"
            idle(models)
            assert not (store.root / MODEL_ID / REVISION).exists()
            expected = [MODEL_ID, "whistle"]
        else:
            models.select(MODEL_ID)
            expected = ["whistle", MODEL_ID]
        release.set()
        finished.wait(2, exactly=True)
        assert calls == expected
        assert committed_models == [MODEL_TAG if kind == MODEL_ID else "whistle" for kind in expected]
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


def test_quit_during_stalled_download_does_not_wait_for_network():
    # Keep the read blocked until AFTER the interpreter has exited. A joined
    # worker (including a non-daemon executor) cannot complete this ordering.
    script = """
import os, sys, threading
from cassini_talk.speech_models import SpeechModels
class Store:
    def _directory(self, *args):
        from pathlib import Path
        return Path("/nonexistent-cassini-model")
    def fetch_manifest(self, **kwargs):
        entered.set()
        os.read(int(sys.argv[1]), 1)
        raise AssertionError("The parent released the network read before exit")
entered = threading.Event()
m = SpeechModels("whistle", lambda _:None, lambda:None, store=Store())
m._inspected.wait()
assert not m.snapshot()["busy"]
assert m.download()
entered.wait()
m.close()
assert m._thread.is_alive(), "The artificial network read must still be held"
assert m.cancelled.is_set() and m.download_cancelled.is_set()
print("closed-with-download-held", flush=True)
"""
    read_fd, held_write_fd = os.pipe()
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(read_fd)],
            pass_fds=(read_fd,),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,  # Outer hang guard, not a shutdown performance budget.
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
        )
        assert result.stdout.strip() == "closed-with-download-held"
    finally:
        os.close(read_fd)
        os.close(held_write_fd)


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
        idle(models)
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
    idle(models)
    assert not models.snapshot()["error"] and models.snapshot()["selected"] == "whistle"
    monkeypatch.setattr(models.store, "fetch_manifest", original)
    downloaded(models)


@pytest.mark.parametrize("damage", ["receipt", "notice", "extra", "weight"])
def test_try_again_can_recover_a_damaged_published_revision(model_fixture, damage):
    models, store, _, _ = model_fixture
    downloaded(models)
    directory = store.root / MODEL_ID / REVISION
    name = {
        "receipt": ".cassini-model.json",
        "notice": ".cassini-NOTICE.txt",
        "extra": "unexpected",
        "weight": "weights.onnx",
    }[damage]
    (directory / name).write_bytes(b"damaged")
    assert models.download()
    idle(models)
    assert models.snapshot()["error"] and not models.snapshot()["downloaded"]
    assert models.snapshot()["selected"] == "whistle"
    assert not directory.exists()
    downloaded(models)
    assert store.installed(MODEL_ID, REVISION)


@pytest.mark.parametrize("damage", ["receipt", "notice", "extra"])
def test_initial_inspection_rejects_missing_attribution_or_unchecked_files(model_fixture, damage):
    models, store, _, persisted = model_fixture
    downloaded(models)
    models.close()
    directory = store.root / MODEL_ID / REVISION
    if damage == "extra":
        (directory / "unexpected").write_bytes(b"unchecked")
    else:
        (directory / {"receipt": ".cassini-model.json", "notice": ".cassini-NOTICE.txt"}[damage]).unlink()
    second = SpeechModels(MODEL_ID, persisted.append, lambda: None, store=store)
    try:
        idle(second)
        assert not second.snapshot()["downloaded"] and second.snapshot()["selected"] == "whistle"
        assert not directory.exists()
    finally:
        second.close()


def test_cancel_after_atomic_publish_keeps_completed_download_visible(model_fixture, monkeypatch):
    models, store, _, _ = model_fixture
    original = store.download

    def publish(*args, **kwargs):
        directory = original(*args, **kwargs)
        kwargs["cancelled"].set()
        return directory

    monkeypatch.setattr(store, "download", publish)
    downloaded(models)
    assert store.installed(MODEL_ID, REVISION) and models.select(MODEL_ID)
    assert models.remove()
    idle(models)
    assert not (store.root / MODEL_ID / REVISION).exists()


def test_valid_revision_restores_about_notice_before_model_use(model_fixture):
    from cassini_talk.model_download import has_downloaded_models

    models, store, _, _ = model_fixture
    downloaded(models)
    models.close()
    (store.root / "NOTICE.txt").unlink()
    second = SpeechModels(MODEL_ID, lambda _: None, lambda: None, store=store)
    try:
        idle(second)
        assert second.snapshot()["downloaded"] and has_downloaded_models(store.root)
        assert (store.root / "NOTICE.txt").read_bytes() == test_model_download.NOTICE
    finally:
        second.close()


def test_verified_startup_promotes_automatic_default(model_fixture):
    models, store, remote, _ = model_fixture
    downloaded(models)
    before = len(remote.requests)
    persisted = []
    second = SpeechModels("whistle", persisted.append, lambda: None, store=store)
    try:
        idle(second)
        assert second.snapshot()["downloaded"]
        assert second.choice_for_job() == MODEL_ID
        assert persisted == [MODEL_ID]
        assert second.snapshot()["notice"] == "models.parakeetUltra.nowDefault"
        assert len(remote.requests) == before
    finally:
        second.close()


def test_explicit_whistle_choice_survives_download_and_restart(model_fixture):
    models, store, _, _ = model_fixture
    preferences = []
    models.persist_preference = preferences.append
    assert models.select("whistle")
    downloaded(models)
    assert models.snapshot()["selected"] == "whistle"
    assert preferences == ["whistle"]
    second = SpeechModels("whistle", lambda _: None, lambda: None, store=store, preference=preferences[-1])
    try:
        idle(second)
        assert second.snapshot()["downloaded"]
        assert second.choice_for_job() == "whistle"
        assert second.snapshot()["notice"] is None
    finally:
        second.close()


def test_initial_verification_holds_job_choice_until_verified(model_fixture, monkeypatch):
    models, store, _, _ = model_fixture
    downloaded(models)
    entered, release, chosen = threading.Event(), threading.Event(), threading.Event()
    original = SpeechModels._verified

    def held(self):
        entered.set()
        durable(release, "release initial verification")
        return original(self)

    monkeypatch.setattr(SpeechModels, "_verified", held)
    second = SpeechModels("whistle", lambda _: None, lambda: None, store=store)
    result = []

    def choose():
        result.append(second.choice_for_job())
        chosen.set()

    waiter = threading.Thread(target=choose)
    try:
        durable(entered, "initial verification entered")
        waiter.start()
        assert not chosen.is_set()
        release.set()
        durable(chosen, "job choice after verification")
        assert result == [MODEL_ID]
        idle(second)
    finally:
        release.set()
        second.close()
        waiter.join(2)
        assert not waiter.is_alive()


def test_suggestion_refresh_is_off_thread_and_updates_when_disk_changes(model_fixture, monkeypatch):
    models, store, _, _ = model_fixture
    from durable import Changes

    from cassini_talk.parakeet_suggestion import offer

    changes = Changes()
    models.changed = changes
    models.capable = lambda: True
    enough = store.disk_usage(store.root.parent)
    monkeypatch.setattr(store, "disk_usage", lambda _: enough._replace(free=0))
    models.refresh_suggestion()
    models._eligibility_thread.join(2)
    assert not models.snapshot()["suggestible"]
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()

    def held(_):
        assert threading.get_ident() != caller
        entered.set()
        durable(release, "release disk capability check")
        return enough._replace(free=10**15, total=10**12)

    monkeypatch.setattr(store, "disk_usage", held)
    try:
        models.refresh_suggestion()
        durable(entered, "disk check entered")
        assert not offer(models.snapshot(), False)
        release.set()
        changes.wait(lambda: offer(models.snapshot(), False))
        assert not offer(models.snapshot(), True)
    finally:
        release.set()
        models._eligibility_thread.join(2)
        assert not models._eligibility_thread.is_alive()


@pytest.mark.parametrize("installed", [True, False])
def test_legacy_whistle_choice_requires_already_verified_parakeet(model_fixture, installed):
    models, store, _, _ = model_fixture
    if installed:
        downloaded(models)
    preferences = []
    second = SpeechModels(
        "whistle",
        lambda _: None,
        lambda: None,
        store=store,
        legacy_whistle=True,
        persist_preference=preferences.append,
    )
    try:
        idle(second)
        if installed:
            assert second.snapshot()["downloaded"]
            assert second.choice_for_job() == "whistle"
            assert preferences == ["whistle"]
        else:
            assert not second.snapshot()["downloaded"]
            downloaded(second)
            assert second.choice_for_job() == MODEL_ID
            assert preferences == []
    finally:
        second.close()


def legacy_install(store, revision):
    directory = store.root / parakeet_model.LEGACY_MODEL_ID / revision
    directory.mkdir(parents=True)
    (directory / "legacy.onnx").write_bytes(b"legacy-model")
    return directory


@pytest.mark.parametrize("revision", parakeet_model.LEGACY_REVISIONS)
def test_v2_retires_only_known_legacy_installs_after_verified_publication(model_fixture, monkeypatch, revision):
    models, store, remote, persisted = model_fixture
    legacy = legacy_install(store, revision)
    unrelated = store.root / "another-model" / ("c" * 64)
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_bytes(b"unrelated")
    remove = store.remove
    retired = []

    def checked_remove(model_id, source, **kwargs):
        if model_id == parakeet_model.LEGACY_MODEL_ID:
            assert store.installed(MODEL_ID, REVISION)
            retired.append(source)
        return remove(model_id, source, **kwargs)

    monkeypatch.setattr(store, "remove", checked_remove)
    assert models.download()
    models._thread.join()  # Completion event; the suite's outer watchdog guards hangs.
    assert models.snapshot()["downloaded"] and not models.snapshot()["error"]
    assert models.choice_for_job() == MODEL_ID and persisted[-1] == MODEL_ID
    assert not legacy.exists() and revision in retired
    assert (unrelated / "keep").read_bytes() == b"unrelated"


def test_missing_or_failed_v2_download_preserves_legacy_install(model_fixture):
    models, store, remote, persisted = model_fixture
    legacy = legacy_install(store, parakeet_model.LEGACY_REVISIONS[0])
    assert not models.snapshot()["downloaded"]
    remote.mode = "truncate"
    assert models.download()
    models._thread.join()
    assert models.snapshot()["error"] and not models.snapshot()["downloaded"]
    assert (legacy / "legacy.onnx").read_bytes() == b"legacy-model"


def test_verified_v2_startup_retires_legacy_offline_and_resolves_old_preference(model_fixture):
    models, store, remote, persisted = model_fixture
    assert models.download()
    models._thread.join()
    legacy = legacy_install(store, parakeet_model.LEGACY_REVISIONS[0])
    models.close()
    before = len(remote.requests)
    replacement = SpeechModels(
        parakeet_model.LEGACY_MODEL_ID,
        persisted.append,
        lambda: None,
        store=store,
        preference=parakeet_model.LEGACY_MODEL_ID,
    )
    try:
        replacement._thread.join()
        assert replacement.snapshot()["downloaded"] and replacement.choice_for_job() == MODEL_ID
        assert not legacy.exists() and len(remote.requests) == before
    finally:
        replacement.close()


def test_invalid_v2_startup_keeps_legacy_model(model_fixture):
    models, store, remote, persisted = model_fixture
    assert models.download()
    models._thread.join()
    legacy = legacy_install(store, parakeet_model.LEGACY_REVISIONS[0])
    (store.root / MODEL_ID / REVISION / "weights.onnx").write_bytes(b"corrupt")
    models.close()
    replacement = SpeechModels(MODEL_ID, persisted.append, lambda: None, store=store)
    try:
        replacement._thread.join()
        assert replacement.choice_for_job() == "whistle" and not replacement.snapshot()["downloaded"]
        assert (legacy / "legacy.onnx").read_bytes() == b"legacy-model"
    finally:
        replacement.close()


def test_legacy_cleanup_failure_keeps_verified_v2_usable_and_retries_at_startup(model_fixture, monkeypatch):
    models, store, remote, persisted = model_fixture
    legacy = legacy_install(store, parakeet_model.LEGACY_REVISIONS[0])
    remove = store.remove

    def denied(model_id, source, **kwargs):
        if model_id == parakeet_model.LEGACY_MODEL_ID:
            raise PermissionError("fixture: legacy directory is read-only")
        return remove(model_id, source, **kwargs)

    monkeypatch.setattr(store, "remove", denied)
    assert models.download()
    models._thread.join()
    assert models.snapshot()["downloaded"] and not models.snapshot()["error"]
    assert models.choice_for_job() == MODEL_ID and legacy.exists()
    models.close()
    monkeypatch.setattr(store, "remove", remove)
    before = len(remote.requests)
    replacement = SpeechModels(MODEL_ID, persisted.append, lambda: None, store=store)
    try:
        replacement._thread.join()
        assert replacement.choice_for_job() == MODEL_ID and not legacy.exists()
        assert len(remote.requests) == before
    finally:
        replacement.close()
