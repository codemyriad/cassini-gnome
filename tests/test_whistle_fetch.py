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


def _fake_onnxruntime_wheel(path, extension):
    """A wheel shaped like the pinned one: kept, dropped and RECORD files."""
    import base64
    import zipfile

    files = {
        "onnxruntime/__init__.py": b"__version__ = '1.31.0'\n",
        "onnxruntime/LICENSE": (ROOT / "whistle/LICENSE-ONNXRuntime-MIT.txt").read_bytes(),
        "onnxruntime/capi/" + extension: b"\x7fELF extension",
        "onnxruntime/capi/libonnxruntime.so.1.31.0": b"\x7fELF C API",
        "onnxruntime/tools/symbolic_shape_infer.py": b"import sympy\n",
        "onnxruntime/transformers/__init__.py": b"",
        "onnxruntime/quantization/__init__.py": b"",
        "onnxruntime/backend/backend.py": b"import onnx\nfrom packaging.version import Version\n",
        "onnxruntime-1.31.0.dist-info/METADATA": b"Requires-Dist: sympy\n",
    }
    record = "".join(
        f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
        for name, data in files.items()
    )
    record += "onnxruntime-1.31.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("onnxruntime-1.31.0.dist-info/RECORD", record)
    return path


def test_onnxruntime_is_unpacked_without_its_model_tools_and_checked_against_record(tmp_path, monkeypatch):
    import sysconfig

    from cassini_talk.whistle_worker import onnxruntime_dir, verify_onnxruntime

    extension = "onnxruntime_pybind11_state" + sysconfig.get_config_var("EXT_SUFFIX")
    wheel = _fake_onnxruntime_wheel(tmp_path / "fake.whl", extension)
    monkeypatch.setattr(fetch, "cached", lambda *_a, **_k: wheel)
    out = tmp_path / "lib/cassini-talk/python"
    fetch.build_onnxruntime(out, tmp_path / "cache", "cp314", offline=True)
    installed = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert installed == [
        "onnxruntime-1.31.0.dist-info/METADATA",
        "onnxruntime-1.31.0.dist-info/RECORD",
        "onnxruntime/LICENSE",
        "onnxruntime/__init__.py",
        "onnxruntime/capi/" + extension,
    ]
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in out.rglob("*") if p.is_file())
    config = tmp_path / "paths.json"
    config.write_text(json.dumps({"onnxruntime": str(out)}))
    assert onnxruntime_dir(config) == out
    # A byte-compiled cache is fine; anything else changed or added is not.
    (out / "onnxruntime/__pycache__").mkdir()
    (out / "onnxruntime/__pycache__/__init__.cpython-314.pyc").write_bytes(b"cache")
    assert verify_onnxruntime(out) == out.resolve()
    (out / "onnxruntime/capi" / extension).write_bytes(b"\x7fELF stripped")
    with pytest.raises(ValueError, match="SHA256 mismatch: onnxruntime/capi/"):
        verify_onnxruntime(out)
    (out / "onnxruntime/capi" / extension).write_bytes(b"\x7fELF extension")
    (out / "onnxruntime/extra.py").write_bytes(b"")
    with pytest.raises(ValueError, match="Unexpected ONNX Runtime file: onnxruntime/extra.py"):
        verify_onnxruntime(out)


def test_onnxruntime_needs_a_wheel_pinned_for_the_python_abi(tmp_path):
    with pytest.raises(ValueError, match="No pinned ONNX Runtime wheel for cp310"):
        fetch.build_onnxruntime(tmp_path / "out", tmp_path, "cp310", offline=True)
    info = PINS["onnxruntime"]["wheels"]["cp313"]
    with pytest.raises(FileNotFoundError) as error:
        fetch.build_onnxruntime(tmp_path / "out", tmp_path, "cp313", offline=True)
    assert info["url"] in str(error.value) and info["sha256"] in str(error.value)


def test_private_onnxruntime_is_used_only_for_the_matching_python(tmp_path):
    from cassini_talk.whistle_worker import onnxruntime_dir

    config = tmp_path / "paths.json"
    assert onnxruntime_dir(config) is None  # a source run: no paths.json
    config.write_text(json.dumps({"onnxruntime": ""}))
    assert onnxruntime_dir(config) is None  # built with -Dwhistle=disabled
    capi = tmp_path / "python/onnxruntime/capi"
    capi.mkdir(parents=True)
    config.write_text(json.dumps({"onnxruntime": str(tmp_path / "python")}))
    (capi / "onnxruntime_pybind11_state.cpython-30-x86_64-linux-gnu.so").write_bytes(b"")
    assert onnxruntime_dir(config) is None  # another Python's extension
