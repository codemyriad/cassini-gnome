# SPDX-License-Identifier: GPL-3.0-or-later
"""whistle/fetch.py: package builds download once, then build offline, failing closed."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("whistle_fetch", ROOT / "whistle/fetch.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)
PINS = json.loads((ROOT / "whistle/pins.json").read_text())


def test_offline_build_names_the_missing_artifact_url_and_sha256_and_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *a, **k: pytest.fail("offline build downloaded"))
    with pytest.raises(FileNotFoundError) as error:
        fetch.fetch(tmp_path / "bundle", tmp_path / "cache", offline=True)
    info = PINS["artifacts"]["whistle.cact"]
    assert info["url"] in str(error.value) and info["sha256"] in str(error.value)
    assert not (tmp_path / "cache").exists() and not (tmp_path / "bundle").exists()


def test_offline_build_rejects_a_corrupt_cached_artifact(tmp_path):
    (tmp_path / "whistle.cact").write_bytes(b"not the pinned weights")
    with pytest.raises(ValueError, match="SHA256 mismatch: whistle.cact"):
        fetch.fill_cache(tmp_path, offline=True)


def _fake_download(monkeypatch, responses):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)
    return calls


def test_download_retries_transient_failures_then_checks_the_hash(tmp_path, monkeypatch):
    payload = b"artifact"
    info = dict(url="https://example.invalid/a", sha256=hashlib.sha256(payload).hexdigest())
    calls = _fake_download(monkeypatch, [TimeoutError("read timeout"), ConnectionResetError("reset"), payload])
    fetch.download("a", info, tmp_path / "a")
    assert (tmp_path / "a").read_bytes() == payload and len(calls) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["a"]


def test_download_gives_up_after_three_attempts(tmp_path, monkeypatch):
    info = dict(url="https://example.invalid/a", sha256="0" * 64)
    _fake_download(monkeypatch, [TimeoutError("t")] * 3)
    with pytest.raises(RuntimeError, match="Could not download a from https://example.invalid/a"):
        fetch.download("a", info, tmp_path / "a")
    assert not list(tmp_path.iterdir())


def test_download_never_retries_or_keeps_a_hash_mismatch(tmp_path, monkeypatch):
    info = dict(url="https://example.invalid/a", sha256="0" * 64)
    calls = _fake_download(monkeypatch, [b"tampered", b"tampered"])
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        fetch.download("a", info, tmp_path / "a")
    assert len(calls) == 1 and not list(tmp_path.iterdir())
