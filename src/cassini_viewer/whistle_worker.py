# SPDX-License-Identifier: GPL-3.0-or-later
"""File-only Whistle spike worker. No engine import occurs in the GTK process."""

import base64
import csv
import hashlib
import importlib
import json
import os
import platform
import sys
import sysconfig
import time
import wave
from pathlib import Path

PRIVACY = {"NEEDLE_TELEMETRY": "0", "DO_NOT_TRACK": "1", "HF_HUB_OFFLINE": "1", "ORT_DISABLE_TELEMETRY": "1"}
MAX_SECONDS = 30


def guarded_load(loader):
    for name, value in PRIVACY.items():
        if os.environ.get(name) != value:
            raise RuntimeError(f"Whistle privacy guard: {name} must be {value} before loading")
    return loader()


def verify_bundle(bundle):
    bundle = Path(bundle).resolve()
    pins = json.loads((bundle / "pins.json").read_text())
    manifest = json.loads((bundle / "manifest.json").read_text())
    if pins["platform"] != "manylinux2014_x86_64" or platform.machine() != "x86_64" or sys.platform != "linux":
        raise RuntimeError("This spike bundles only the pinned Linux glibc x86_64 engine")
    for name, expected in pins["installed"].items():
        if manifest.get(name) != expected:
            raise ValueError("Whistle manifest differs from pins")
    if set(manifest) != {*pins["installed"], "libcassini_whistle_cpu.so"}:
        raise ValueError("Unexpected Whistle bundle manifest")
    for name, expected in manifest.items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Whistle bundle SHA256 mismatch: {name}")
    return bundle


def onnxruntime_dir(config=None):
    """The app's private ONNX Runtime for this Python, or None.

    Packages install the pinned upstream wheel, unpacked, in a private
    directory (meson writes its path to paths.json next to this module); only
    the recognition child puts it on sys.path. Without one for this Python's
    ABI (a source run, or a Python upgrade the package was not rebuilt for),
    the child uses whatever onnxruntime the Python itself has. Import-free,
    so the GTK process can ask too.
    """
    config = Path(config) if config else Path(__file__).with_name("paths.json")
    try:
        configured = json.loads(config.read_text())["onnxruntime"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not configured:
        return None
    path = Path(configured)
    extension = path / "onnxruntime" / "capi" / ("onnxruntime_pybind11_state" + sysconfig.get_config_var("EXT_SUFFIX"))
    return path if extension.is_file() else None


def verify_onnxruntime(path):
    """Check every file of the private ONNX Runtime against its wheel's RECORD."""
    path = Path(path).resolve()
    records = list(path.glob("onnxruntime-*.dist-info/RECORD"))
    if len(records) != 1:
        raise ValueError(f"Expected one onnxruntime dist-info RECORD in {path}")
    record = records[0].relative_to(path).as_posix()
    expected = {}
    for row in csv.reader(records[0].read_text().splitlines()):
        if row and row[0] != record:
            algorithm, _, digest = row[1].partition("=")
            if algorithm != "sha256":
                raise ValueError(f"Unexpected ONNX Runtime RECORD hash for {row[0]}")
            expected[row[0]] = digest
    for file in sorted(path.rglob("*")):
        # Byte-compiled caches are not in RECORD; a package may add them.
        if file.is_dir() or "__pycache__" in file.parts:
            continue
        name = file.relative_to(path).as_posix()
        if name == record:
            continue
        if name not in expected:
            raise ValueError(f"Unexpected ONNX Runtime file: {name}")
        digest = base64.urlsafe_b64encode(hashlib.sha256(file.read_bytes()).digest()).rstrip(b"=").decode()
        if digest != expected[name]:
            raise ValueError(f"ONNX Runtime SHA256 mismatch: {name}")
    if not (path / "onnxruntime/__init__.py").is_file() or not list(
        path.glob("onnxruntime/capi/onnxruntime_pybind11_state.*.so")
    ):
        raise ValueError(f"Incomplete ONNX Runtime in {path}")
    return path


def block_network(event, _args):
    if event.startswith("socket."):
        raise RuntimeError("Network access is disabled in the Whistle worker")


def emit(kind, **values):
    print(json.dumps(dict(type=kind, **values), ensure_ascii=False), flush=True)


def main(bundle):
    # Guard wraps the actual wrapper/model constructor, not just the command entry.
    bundle = verify_bundle(bundle)
    runtime = onnxruntime_dir()
    if runtime is not None:
        sys.path.insert(0, str(verify_onnxruntime(runtime)))
    os.environ["NEEDLE3_LIB_PATH"] = str(bundle / "libneedle3.so")
    sys.addaudithook(block_network)
    sys.path.insert(0, str(bundle / "wrapper.whl"))
    model = guarded_load(lambda: importlib.import_module("needle").Whistle(weights=bundle / "whistle.cact"))
    emit("ready", pid=os.getpid(), threads=len(list(Path("/proc/self/task").iterdir())))
    for line in iter(lambda: sys.stdin.buffer.readline(512 * 1024 + 1), b""):
        if len(line) > 512 * 1024:
            raise ValueError("Worker request exceeds limit")
        message = json.loads(line)
        if message.get("type") == "quit":
            return 0
        if message.get("type") == "pipeline":
            tracks = message.get("tracks")
            if not isinstance(tracks, list) or not 1 <= len(tracks) <= 64:
                raise ValueError("Expected one to 64 capture tracks")
            for track in tracks:
                if (
                    not isinstance(track, dict)
                    or not isinstance(track.get("id"), str)
                    or not isinstance(track.get("path"), str)
                ):
                    raise ValueError("Invalid capture track")
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from cassini_viewer.speech_pipeline import process_tracks
            from cassini_viewer.worker_diarization import complete, estimate

            options = message.get("diarization")
            summary = process_tracks(
                model, tracks, bundle / "silero_vad.onnx", emit, extra_seconds=estimate(tracks, options)
            )
            summary = complete(summary, tracks, options, emit)
            emit("complete", **summary)
            continue
        if message.get("type") != "transcribe" or not isinstance(message.get("path"), str):
            raise ValueError("Expected transcribe request with local WAV path")
        path = message["path"]
        with wave.open(path, "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (
                1,
                2,
                16000,
                "NONE",
            ) or not 0 < audio.getnframes() <= MAX_SECONDS * 16000:
                raise ValueError("Expected at most 30 seconds of PCM16 mono 16 kHz")
        started = time.monotonic()
        result = model.transcribe(path, language=None)
        emit("result", text=result["text"], pid=os.getpid(), seconds=time.monotonic() - started)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as error:
        emit("error", message=str(error))
        sys.exit(1)
