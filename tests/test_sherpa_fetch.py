# SPDX-License-Identifier: GPL-3.0-or-later
"""sherpa/fetch.py: a transfer that fails is retried, capped and announced (D66); a pin mismatch never is."""

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sherpa_fetch", ROOT / "sherpa/fetch.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def _fake_download(monkeypatch, responses):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)  # The backoff is not under test.
    return calls


def _pin(payload):
    return dict(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())


def test_download_retries_transient_failures_with_a_warning_then_checks_the_pin(tmp_path, monkeypatch, capsys):
    payload = b"library"
    calls = _fake_download(monkeypatch, [TimeoutError("read timeout"), ConnectionResetError("reset"), payload])
    path = fetch.cached(tmp_path, "lib.so", "https://example.invalid/lib.so", offline=False, expected=_pin(payload))
    assert path.read_bytes() == payload and len(calls) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["lib.so"]
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.startswith("::warning::")]
    assert len(warnings) == 2 and "attempt 1 of 3" in warnings[0] and "attempt 2 of 3" in warnings[1]


def test_download_gives_up_after_three_attempts(tmp_path, monkeypatch):
    calls = _fake_download(monkeypatch, [TimeoutError("t")] * 4)
    with pytest.raises(RuntimeError, match="Could not download lib.so from https://example.invalid/lib.so"):
        fetch.cached(tmp_path, "lib.so", "https://example.invalid/lib.so", offline=False, expected=_pin(b"x"))
    assert len(calls) == 3 and not list(tmp_path.iterdir())


def test_download_never_retries_or_keeps_a_pin_mismatch(tmp_path, monkeypatch, capsys):
    calls = _fake_download(monkeypatch, [b"tampered", b"tampered"])
    with pytest.raises(ValueError, match="Pinned runtime differs: lib.so"):
        fetch.cached(tmp_path, "lib.so", "https://example.invalid/lib.so", offline=False, expected=_pin(b"library"))
    assert len(calls) == 1 and not list(tmp_path.iterdir())
    assert "::warning::" not in capsys.readouterr().err


def test_offline_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *a, **k: pytest.fail("offline build downloaded"))
    with pytest.raises(FileNotFoundError, match="run build-aux/sherpa-cache.sh"):
        fetch.cached(tmp_path, "lib.so", "https://example.invalid/lib.so", offline=True)
