# SPDX-License-Identifier: GPL-3.0-or-later
"""D25 immutable dist pins. No engine or model is loaded in this module."""

import hashlib
import json
import os
from concurrent.futures import CancelledError
from pathlib import Path

from .model_download import model_root
from .received import regular_input

MODEL_ID = "campplus-voxceleb-16k"
PIN = json.loads(Path(__file__).with_name("models.json").read_text())["models"][MODEL_ID]
REVISION = PIN["source_sha256"]
MODEL_TAG = MODEL_ID + "@" + REVISION
FILES = {name: (p["size"], p["sha256"], p["downloadBytes"]) for name, p in PIN["files"].items()}
DOWNLOAD_BYTES = sum(p[2] for p in FILES.values())
INSTALLED_BYTES = sum(p[0] for p in FILES.values())


def model_path(root=None):
    return Path(root or model_root()) / MODEL_ID / REVISION


def check_manifest(model):
    actual = {
        f.path: (f.artifact.uncompressed_size, f.artifact.uncompressed_sha256, f.artifact.size) for f in model.files
    }
    if model.id != MODEL_ID or model.source_sha256 != REVISION or actual != FILES or len(model.files) != len(FILES):
        raise ValueError("Distribution manifest differs from the D25 CAM++ pin")
    if model.license != PIN["license"]:
        raise ValueError("Distribution licence differs from the approved CAM++ revision")
    for file in model.files:
        if file.artifact.sha256 != PIN["files"][file.path]["artifactSHA256"]:
            raise ValueError("Compressed CAM++ artifact differs from the D25 pin")
    return model


def verify_model(directory, *, cancelled=None):
    signature = []
    for name, (size, expected, _compressed) in FILES.items():
        with regular_input(Path(directory) / name) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size != size:
                raise ValueError("CAM++ model size mismatch: " + name)
            digest = hashlib.sha256()
            while block := stream.read(128 * 1024):
                if cancelled is not None and cancelled.is_set():
                    raise CancelledError()
                digest.update(block)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ) or digest.hexdigest() != expected:
                raise ValueError("CAM++ model SHA256 mismatch: " + name)
            signature.append((name, after.st_ino, after.st_size, after.st_mtime_ns))
    return tuple(signature)
