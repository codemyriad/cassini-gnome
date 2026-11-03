# SPDX-License-Identifier: GPL-3.0-or-later
"""tests/parakeet/prepare_model.py: the package builds' D16 fixture download.

A transfer that fails is retried, capped and announced (D66); an integrity
failure or a full disk never is. No network: the store is scripted.
"""

import runpy
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cassini_talk.model_download as model_download
import cassini_talk.parakeet_model as parakeet_model

SCRIPT = Path(__file__).resolve().parent / "parakeet/prepare_model.py"


class Store:
    """A ModelStore whose downloads fail with the scripted errors, in order."""

    def __init__(self, failures):
        self.failures, self.downloads, self.done = list(failures), 0, False

    def __call__(self, *_args, **_kwargs):
        return self  # Stands in for the class: the script constructs it once.

    def fetch_manifest(self):
        return SimpleNamespace(model=lambda *_: None)

    def download(self, *_args, **_kwargs):
        self.downloads += 1
        if self.failures:
            raise self.failures.pop(0)
        self.done = True


def prepare(tmp_path, monkeypatch, failures):
    store = Store(failures)

    def verify(_path, **_kwargs):
        if not store.done:
            raise FileNotFoundError("cache miss")

    monkeypatch.setattr(model_download, "ModelStore", store)
    monkeypatch.setattr(parakeet_model, "verify_model", verify)
    monkeypatch.setattr(parakeet_model, "check_manifest", lambda _model: None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)  # The backoff is not under test.
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--cache", str(tmp_path)])
    return store


def warnings(capsys):
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("::warning::")]


def test_transient_failures_are_retried_with_a_warning(tmp_path, monkeypatch, capsys):
    store = prepare(tmp_path, monkeypatch, [model_download.DownloadInterrupted("cut"), TimeoutError("slow")])
    runpy.run_path(str(SCRIPT), run_name="__main__")
    assert store.done and store.downloads == 3
    found = warnings(capsys)
    assert len(found) == 2 and "attempt 1 of 3" in found[0] and "attempt 2 of 3" in found[1]


def test_retries_are_capped_at_three_attempts(tmp_path, monkeypatch):
    store = prepare(tmp_path, monkeypatch, [TimeoutError("slow")] * 4)
    with pytest.raises(TimeoutError):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert store.downloads == 3


@pytest.mark.parametrize(
    "failure",
    [
        model_download.IntegrityError("Model file digest differs"),
        model_download.InsufficientSpace(model_download.SpacePlan(1, 1, 1, 1, 1, 0)),
    ],
    ids=["integrity", "space"],
)
def test_integrity_and_space_failures_are_never_retried(tmp_path, monkeypatch, capsys, failure):
    store = prepare(tmp_path, monkeypatch, [failure])
    with pytest.raises(type(failure)):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert store.downloads == 1 and not warnings(capsys)
