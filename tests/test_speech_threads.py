# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognition policy boundaries and the actual child/native configuration."""

import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from cassini_talk import parakeet, speech_threads, whistle_client


@pytest.mark.parametrize(
    "cores,expected", [(None, 2), (1, 2), (2, 2), (3, 2), (4, 2), (5, 3), (8, 6), (10, 8), (64, 8)]
)
def test_policy_uses_usable_cpu_budget_with_minimum_and_cap(monkeypatch, cores, expected):
    monkeypatch.setattr(speech_threads, "available_cpus", lambda: cores or 1)
    assert speech_threads.recognition_threads() == expected


@pytest.mark.parametrize("override,expected", [(None, "6"), ("1", "1"), ("32", "32"), (" 4 ", "4")])
def test_whistle_child_uses_policy_default_but_retains_explicit_override(monkeypatch, tmp_path, override, expected):
    monkeypatch.setattr(speech_threads, "available_cpus", lambda: 8)
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
    monkeypatch.setattr(speech_threads, "available_cpus", lambda: 8)
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


@pytest.mark.parametrize(
    "total,affinity,quota,expected",
    [
        (12, 8, None, 8),
        (12, 4, None, 4),
        (64, 16, 6, 6),
        (4, 16, None, 4),
        (None, 8, None, 8),
        (8, None, 4, 4),
        (None, None, None, 1),
        (8, 0, None, 8),
    ],
)
def test_usable_budget_clamps_reported_count_affinity_and_quota(monkeypatch, total, affinity, quota, expected):
    monkeypatch.setattr(speech_threads.os, "cpu_count", lambda: total)

    def allowed(_pid):
        if affinity is None:
            raise OSError("affinity unavailable")
        return set(range(affinity))

    monkeypatch.setattr(speech_threads.os, "sched_getaffinity", allowed)
    monkeypatch.setattr(speech_threads, "_cgroup_cpu_limit", lambda: quota)
    assert speech_threads.available_cpus() == expected
    assert speech_threads.recognition_threads() == min(8, max(2, expected - 2))


def test_reported_count_still_works_without_an_affinity_api(monkeypatch):
    monkeypatch.setattr(speech_threads.os, "cpu_count", lambda: 8)
    monkeypatch.delattr(speech_threads.os, "sched_getaffinity")
    monkeypatch.setattr(speech_threads, "_cgroup_cpu_limit", lambda: None)
    assert speech_threads.available_cpus() == 8


@pytest.mark.parametrize(
    "root_limit,parent_limit,leaf_limit,expected",
    [
        ("max 100000", "800000 100000", "400000 100000", 4),
        ("200000 100000", "800000 100000", "max 100000", 2),
        ("max 100000", "650000 100000", "max 100000", 6),
        ("max 100000", "max 100000", "50000 100000", 1),
        ("not a quota", "max 100000", "400000 100000", 4),
        ("max 100000", "400000 0", "-1 100000", None),
        ("max 100000", "max 100000", "missing", None),
    ],
)
def test_cgroup_budget_uses_tightest_visible_ancestor_and_floors_fractional_cores(
    tmp_path, root_limit, parent_limit, leaf_limit, expected
):
    root = tmp_path / "cgroup"
    leaf = root / "user.slice/session.scope"
    leaf.mkdir(parents=True)
    for directory, value in ((root, root_limit), (leaf.parent, parent_limit), (leaf, leaf_limit)):
        if value != "missing":
            (directory / "cpu.max").write_text(value)
    proc = tmp_path / "proc-cgroup"
    proc.write_text("3:memory:/ignored\n0::/user.slice/session.scope\n")
    assert speech_threads._cgroup_cpu_limit(root, proc) == expected


@pytest.mark.parametrize("process_path", [None, "/", "/../../outside", "/escape"])
def test_cgroup_root_quota_survives_unavailable_or_namespace_relative_process_paths(tmp_path, process_path):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cpu.max").write_text("400000 100000")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cpu.max").write_text("100000 100000")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    proc = tmp_path / "proc-cgroup"
    if process_path is not None:
        proc.write_text("0::" + process_path + "\n")
    assert speech_threads._cgroup_cpu_limit(root, proc) == 4


def test_missing_cgroup_files_leave_affinity_and_logical_count_in_control(tmp_path):
    assert speech_threads._cgroup_cpu_limit(tmp_path / "missing", tmp_path / "no-proc") is None


@pytest.mark.parametrize("kind", ["parakeet", "whistle"])
def test_workers_lower_priority_before_loading_native_pools(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(sys, "path", list(sys.path))
    from cassini_talk import parakeet_worker, whistle_worker

    events = []

    def priority():
        events.append("priority")

    monkeypatch.setattr(sys, "addaudithook", lambda _hook: None)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"type":"quit"}\n')))
    for key, value in whistle_worker.PRIVACY.items():
        monkeypatch.setenv(key, value)
    worker = parakeet_worker if kind == "parakeet" else whistle_worker
    monkeypatch.setattr(worker, "verify_bundle", Path)
    monkeypatch.setattr(worker, "onnxruntime_dir", lambda: None)
    monkeypatch.setattr(worker, "emit", lambda *_args, **_kwargs: None)

    def load(*_args, **_kwargs):
        events.append("load")
        return SimpleNamespace(version="stub", close=Mock())

    if kind == "parakeet":
        monkeypatch.setattr(worker, "background_worker_priority", priority)
        monkeypatch.setattr(parakeet, "Parakeet", load)
        pipeline = ModuleType("cassini_talk.speech_pipeline")
        pipeline.process_tracks = Mock()
        monkeypatch.setitem(sys.modules, "cassini_talk.speech_pipeline", pipeline)
        assert worker.main(tmp_path, tmp_path, tmp_path) == 0
        pipeline.process_tracks.assert_not_called()
    else:
        monkeypatch.setattr(speech_threads, "background_worker_priority", priority)
        needle = ModuleType("needle")
        needle.Whistle = load
        monkeypatch.setitem(sys.modules, "needle", needle)
        assert worker.main(tmp_path) == 0
    assert events == ["priority", "load"]
