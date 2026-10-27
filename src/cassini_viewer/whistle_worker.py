# SPDX-License-Identifier: GPL-3.0-or-later
"""File-only Whistle spike worker. No engine import occurs in the GTK process."""

import hashlib
import importlib
import json
import os
import platform
import sys
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


def block_network(event, _args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.bind"):
        raise RuntimeError("Network access is disabled in the Whistle worker")


def emit(kind, **values):
    print(json.dumps(dict(type=kind, **values), ensure_ascii=False), flush=True)


def main(bundle):
    # Guard wraps the actual wrapper/model constructor, not just the command entry.
    bundle = verify_bundle(bundle)
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

            summary = process_tracks(model, tracks, bundle / "silero_vad.onnx", emit)
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
