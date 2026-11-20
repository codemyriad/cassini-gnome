# SPDX-License-Identifier: GPL-3.0-or-later
"""Pinned private runtime location and binary verification; never load it on GTK."""

import hashlib
import json
import os
import platform
import sys
from pathlib import Path

from .received import regular_input

PINS = json.loads(Path(__file__).with_name("sherpa-pins.json").read_text())
FILES = {
    Path(name).name: pin
    for name, pin in PINS["sherpa-onnx-go-linux"]["files"].items()
    if not name.endswith("cxx-api.so")
}


def runtime_path():
    if os.environ.get("CASSINI_SHERPA_BUNDLE"):
        return Path(os.environ["CASSINI_SHERPA_BUNDLE"])
    try:
        value = json.loads(Path(__file__).with_name("paths.json").read_text()).get("sherpa")
    except (OSError, ValueError):
        value = None
    return Path(value) if value else Path(__file__).resolve().parents[2] / "sherpa/bundle"


def available():
    return (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and all((runtime_path() / name).is_file() for name in FILES)
    )


def verify_runtime_with_signature(directory):
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError("The pinned Parakeet runtime requires Linux x86_64")
    directory = Path(directory).resolve(strict=True)
    signature = []
    for name, expected in FILES.items():
        with regular_input(directory / name) as stream:
            before = os.fstat(stream.fileno())
            if (
                before.st_size != expected["size"]
                or hashlib.file_digest(stream, "sha256").hexdigest() != expected["sha256"]
            ):
                raise ValueError("Parakeet runtime SHA256 mismatch: " + name)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Parakeet runtime changed during verification: " + name)
            signature.append((name, after.st_ino, after.st_size, after.st_mtime_ns))
    return directory, tuple(signature)


def verify_runtime(directory):
    verified, _signature = verify_runtime_with_signature(directory)
    return verified


def runtime_signature_matches(directory, expected_signature) -> bool:
    if not isinstance(expected_signature, tuple) or len(expected_signature) != len(FILES):
        return False
    try:
        resolved = Path(directory).resolve(strict=True)
        actual = []
        for name in FILES:
            with regular_input(resolved / name) as stream:
                info = os.fstat(stream.fileno())
            actual.append((name, info.st_ino, info.st_size, info.st_mtime_ns))
    except (OSError, ValueError):
        return False
    return tuple(actual) == expected_signature
