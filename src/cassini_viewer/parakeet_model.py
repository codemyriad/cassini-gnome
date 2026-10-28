# SPDX-License-Identifier: GPL-3.0-or-later
"""D16 model identity and raw file pins, independent of unsigned download receipts."""

import hashlib
import os
from concurrent.futures import CancelledError
from pathlib import Path

from .model_download import model_root
from .received import regular_input

MODEL_ID = "parakeet-ultra-0.6b-int8"
REVISION = "18cb623004dd6c15999b93cb70b14d8a1f6e8f3a2a97ff733f104bfc3494f6a9"
MODEL_TAG = MODEL_ID + "@" + REVISION
FILES = {
    "encoder.int8.onnx": (652282922, "299617fefd4664d1c115b691dcb6413cabcdbcc694cdf3cb4464b72c5fc3ae69", 452306705),
    "decoder.int8.onnx": (11845274, "1fab98fe6c12aded87d2da66272cc9e148d0a0044ce3850a12fe56302ec4a922", 8841708),
    "joiner.int8.onnx": (6355277, "8a71aaccfdba3d451775507a889afd2257d2e754463fd81095a8c8f290f924c2", 3714640),
    "tokens.txt": (93939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d", 45831),
}
DOWNLOAD_BYTES = sum(value[2] for value in FILES.values())
INSTALLED_BYTES = sum(value[0] for value in FILES.values())


def model_path(root=None):
    return Path(root or model_root()) / MODEL_ID / REVISION


def check_manifest(model):
    actual = {
        f.path: (f.artifact.uncompressed_size, f.artifact.uncompressed_sha256, f.artifact.size) for f in model.files
    }
    if model.id != MODEL_ID or model.source_sha256 != REVISION or actual != FILES or len(model.files) != len(FILES):
        raise ValueError("Distribution manifest differs from the D16 Parakeet pin")
    return model


def verify_model(directory, *, cancelled=None):
    signature = []
    for name, (size, sha, _compressed) in FILES.items():
        with regular_input(Path(directory) / name) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size != size:
                raise ValueError("Parakeet model size mismatch: " + name)
            digest = hashlib.sha256()
            while block := stream.read(128 * 1024):
                if cancelled is not None and cancelled.is_set():
                    raise CancelledError()
                digest.update(block)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or digest.hexdigest() != sha:
                raise ValueError("Parakeet model SHA256 mismatch: " + name)
            signature.append((name, after.st_ino, after.st_size, after.st_mtime_ns))
    return tuple(signature)
