# SPDX-License-Identifier: GPL-3.0-or-later
"""D16 per-job recognizer. No network; Python ORT 1.31 is only for shared Silero."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cassini_viewer.parakeet_model import MODEL_TAG  # noqa: E402
from cassini_viewer.whistle_worker import PRIVACY, emit, onnxruntime_dir, verify_bundle, verify_onnxruntime  # noqa: E402


def block_network(event, _args):
    if event.startswith("socket."):
        raise RuntimeError("Network access is disabled in the Parakeet worker")


def main(bundle, model_path, runtime_path):
    if any(os.environ.get(key) != value for key, value in PRIVACY.items()):
        raise RuntimeError("Speech privacy flags must be set before loading Parakeet")
    bundle = verify_bundle(bundle)
    runtime = onnxruntime_dir()
    if runtime is not None:
        sys.path.insert(0, str(verify_onnxruntime(runtime)))
    sys.addaudithook(block_network)
    from cassini_viewer.parakeet import Parakeet
    from cassini_viewer.speech_pipeline import process_tracks

    model = Parakeet(model_path, runtime_path)
    try:
        emit("ready", pid=os.getpid(), model=MODEL_TAG, version=model.version)
        line = sys.stdin.buffer.readline(512 * 1024 + 1)
        if len(line) > 512 * 1024:
            raise ValueError("Worker request exceeds limit")
        message = json.loads(line)
        if message.get("type") == "quit":
            return 0
        tracks = message.get("tracks")
        if message.get("type") != "pipeline" or not isinstance(tracks, list) or not 1 <= len(tracks) <= 64:
            raise ValueError("Expected one to 64 capture tracks")
        if any(
            not isinstance(t, dict) or not isinstance(t.get("id"), str) or not isinstance(t.get("path"), str)
            for t in tracks
        ):
            raise ValueError("Invalid capture track")
        summary = process_tracks(model, tracks, bundle / "silero_vad.onnx", emit)
        emit("complete", model=MODEL_TAG, **summary)
        return 0
    finally:
        model.close()


if __name__ == "__main__":
    try:
        sys.exit(main(*sys.argv[1:]))
    except Exception as error:
        emit("error", message=str(error))
        sys.exit(1)
