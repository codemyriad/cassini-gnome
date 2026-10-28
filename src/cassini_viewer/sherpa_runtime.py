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


def verify_runtime(directory):
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError("The pinned Parakeet runtime requires Linux x86_64")
    directory = Path(directory).resolve(strict=True)
    for name, expected in FILES.items():
        with regular_input(directory / name) as stream:
            if (
                os.fstat(stream.fileno()).st_size != expected["size"]
                or hashlib.file_digest(stream, "sha256").hexdigest() != expected["sha256"]
            ):
                raise ValueError("Parakeet runtime SHA256 mismatch: " + name)
    return directory
