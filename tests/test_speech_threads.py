# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognition policy boundaries and the actual child/native configuration."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cassini_talk import parakeet, speech_threads, whistle_client


@pytest.mark.parametrize(
    "cores,expected", [(None, 2), (1, 2), (2, 2), (3, 2), (4, 2), (5, 3), (8, 6), (10, 8), (64, 8)]
)
def test_policy_uses_logical_cpu_count_with_minimum_and_cap(monkeypatch, cores, expected):
    monkeypatch.setattr(speech_threads.os, "cpu_count", lambda: cores)
    assert speech_threads.recognition_threads() == expected


@pytest.mark.parametrize("override,expected", [(None, "6"), ("1", "1"), ("32", "32"), (" 4 ", "4")])
def test_whistle_child_uses_policy_default_but_retains_explicit_override(monkeypatch, tmp_path, override, expected):
    monkeypatch.setattr(speech_threads.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(whistle_client, "verify_bundle", Path)
    monkeypatch.delenv("CASSINI_WHISTLE_THREADS", raising=False)
    if override is not None:
        monkeypatch.setenv("CASSINI_WHISTLE_THREADS", override)
    client = whistle_client.WhistleClient(tmp_path)
    environment = client.worker_environment()
    assert environment["CASSINI_WHISTLE_THREADS"] == expected
    assert environment["OMP_NUM_THREADS"] == environment["OPENBLAS_NUM_THREADS"] == "1"
    assert environment["LD_PRELOAD"] == str(tmp_path / "libcassini_whistle_cpu.so")


@pytest.mark.parametrize("override", ["0", "33", "", "not-a-count"])
def test_whistle_rejects_invalid_override_as_before(monkeypatch, tmp_path, override):
    monkeypatch.setattr(whistle_client, "verify_bundle", Path)
    monkeypatch.setenv("CASSINI_WHISTLE_THREADS", override)
    with pytest.raises(ValueError):
        whistle_client.WhistleClient(tmp_path).worker_environment()


def test_parakeet_passes_policy_count_to_the_native_recognizer(monkeypatch, tmp_path):
    monkeypatch.setattr(speech_threads.os, "cpu_count", lambda: 8)
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CASSINI_WHISTLE_THREADS", "1")
    monkeypatch.setattr(parakeet, "verify_model", lambda _: None)
    monkeypatch.setattr(parakeet, "verify_runtime", Path)
    counts = []
    api = SimpleNamespace()
    for name in (
        "GetVersionStr",
        "CreateOfflineRecognizer",
        "DestroyOfflineRecognizer",
        "CreateOfflineStream",
        "DestroyOfflineStream",
        "AcceptWaveformOffline",
        "DecodeOfflineStream",
        "GetOfflineStreamResult",
        "DestroyOfflineRecognizerResult",
    ):
        setattr(api, "SherpaOnnx" + name, Mock())
    api.SherpaOnnxGetVersionStr.return_value = parakeet.PINS["nativeVersion"].encode()
    api.SherpaOnnxCreateOfflineRecognizer.side_effect = lambda config: (
        counts.append(config._obj.model_config.num_threads) or 1
    )
    monkeypatch.setattr(parakeet.C, "CDLL", lambda *_args, **_kwargs: api)
    model = parakeet.Parakeet(tmp_path, tmp_path)
    assert counts == [6]
    model.close()
    api.SherpaOnnxDestroyOfflineRecognizer.assert_called_once_with(1)


@pytest.mark.parametrize("inherited,expected", [(-5, 5), (0, 5), (5, None), (12, None)])
def test_worker_priority_yields_to_capture_and_preserves_lower_inherited_priority(monkeypatch, inherited, expected):
    change = Mock()
    monkeypatch.setattr(speech_threads.os, "getpriority", lambda *_: inherited)
    monkeypatch.setattr(speech_threads.os, "setpriority", change)
    speech_threads.background_worker_priority()
    if expected is None:
        change.assert_not_called()
    else:
        change.assert_called_once_with(speech_threads.os.PRIO_PROCESS, 0, expected)


def test_denied_priority_change_does_not_refuse_a_valid_speech_job(monkeypatch):
    monkeypatch.setattr(speech_threads.os, "getpriority", lambda *_: 0)
    monkeypatch.setattr(speech_threads.os, "setpriority", Mock(side_effect=PermissionError("denied")))
    speech_threads.background_worker_priority()


def test_direct_whistle_worker_bootstraps_package_without_pythonpath(tmp_path):
    worker = Path(whistle_client.__file__).with_name("whistle_worker.py")
    process = subprocess.run(
        [sys.executable, "-I", str(worker), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,  # Hang guard only; no elapsed-time pass condition.
    )
    assert process.returncode == 1
    error = json.loads(process.stdout)
    assert error["type"] == "error"
    assert "pins.json" in error["message"]  # Reached bundle validation, not an import error.
