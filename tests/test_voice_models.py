# SPDX-License-Identifier: GPL-3.0-or-later
"""Real D4 transactions, fake capability replies, no native load in this parent."""

import hashlib

import pytest
import test_model_download
from test_transcription import until

from cassini_viewer import campplus_model, diarization_models
from cassini_viewer.model_download import ModelStore
from cassini_viewer.sherpa_runtime import PINS
from cassini_viewer.voice_models import VoiceModels


class Probe:
    def __init__(self, *_args, **_kwargs):
        self.ready = dict(
            model=campplus_model.MODEL_TAG, sampleRate=16000, dimension=512, version=PINS["nativeVersion"]
        )

    def start(self):
        return self

    def close(self):
        pass


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    remote = test_model_download.remote.__wrapped__()
    server = next(remote)
    try:
        server.manifest["models"][0].update(
            id=campplus_model.MODEL_ID, source_sha256=campplus_model.REVISION, license="Apache-2.0"
        )
        pin = dict(
            size=len(server.data),
            sha256=hashlib.sha256(server.data).hexdigest(),
            downloadBytes=len(server.compressed),
            artifactSHA256=hashlib.sha256(server.compressed).hexdigest(),
        )
        monkeypatch.setattr(
            campplus_model, "FILES", {"weights.onnx": (pin["size"], pin["sha256"], pin["downloadBytes"])}
        )
        monkeypatch.setattr(campplus_model, "PIN", dict(license="Apache-2.0", files={"weights.onnx": pin}))
        monkeypatch.setattr(diarization_models, "verify_runtime", lambda path: path)
        store = ModelStore(tmp_path / "models", origin=server.origin, fixture=True)
        models = VoiceModels(tmp_path / "bundle", lambda: None, store=store, client_factory=Probe)
        until(lambda: not models.snapshot()["busy"])
        yield models, store, server
        models.close()
        models._thread.join(2)
        assert not models._thread.is_alive()
    finally:
        remote.close()


def download(models):
    assert models.download()
    until(lambda: not models.snapshot()["busy"])


def test_optional_model_requires_tell_apart_and_remove_preserves_private_voices(fixture, tmp_path):
    models, store, server = fixture
    assert not server.requests and not models.snapshot()["ready"]
    download(models)
    assert models.snapshot()["ready"] and models.snapshot()["downloaded"]
    notice = store.root / campplus_model.MODEL_ID / campplus_model.REVISION / ".cassini-NOTICE.txt"
    assert notice.is_file()
    models.enabled = lambda: False
    assert models.request(tmp_path, origin="own") is None
    assert models.snapshot()["downloaded"]
    models.enabled = lambda: True
    assert models.request(tmp_path, origin="own")["modelPath"] == str(notice.parent)
    private = tmp_path / "voices/people.json"
    private.parent.mkdir()
    private.write_bytes(b"pending-private-store")
    assert models.remove()
    until(lambda: not models.snapshot()["busy"])
    assert not models.snapshot()["ready"] and not notice.parent.exists()
    assert private.read_bytes() == b"pending-private-store"


@pytest.mark.parametrize(
    ("field", "bad"), [("dimension", 192), ("dimension", 512.0), ("model", "old"), ("version", "other fork")]
)
def test_unsupported_capability_keeps_weights_for_remove_and_retry(fixture, field, bad):
    models, store, _server = fixture

    class Unsupported(Probe):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.ready[field] = bad

    models.client_factory = Unsupported
    download(models)
    assert not models.snapshot()["ready"] and models.snapshot()["error"]
    assert models.snapshot()["downloaded"] and store.installed(campplus_model.MODEL_ID, campplus_model.REVISION)
    models.client_factory = Probe
    assert models.retry()
    until(lambda: not models.snapshot()["busy"])
    assert models.snapshot()["ready"] and not models.snapshot()["error"]


def test_licence_drift_never_fetches_weights(fixture):
    models, _store, server = fixture
    server.manifest["models"][0]["license"] = "CC-BY-4.0"
    download(models)
    assert models.snapshot()["error"] and not models.snapshot()["ready"]
    assert all(path == "/manifest.json" for path, _headers in server.requests)


def test_campplus_constructor_rejects_missing_privacy_flags_before_native_loading(monkeypatch):
    from cassini_viewer.campplus import SpeakerExtractor

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    with pytest.raises(RuntimeError, match="privacy flags"):
        SpeakerExtractor("not-opened", "not-loaded")
