#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build-time fetch only; pinned downloads and installed files fail closed."""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def verify(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"SHA256 mismatch: {path.name}")


def fetch(out, cache):
    pins = json.loads((HERE / "pins.json").read_text())
    cache.mkdir(parents=True, exist_ok=True)
    for name, info in pins["artifacts"].items():
        target = cache / name
        if not target.exists():
            request = urllib.request.Request(info["url"], headers={"Accept-Encoding": "identity"})
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as staged:
                temporary = Path(staged.name)
                try:
                    with urllib.request.urlopen(request, timeout=90) as response:
                        shutil.copyfileobj(response, staged)
                    staged.flush()
                    verify(temporary, info["sha256"])
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        verify(target, info["sha256"])
    if out.exists():
        if json.loads((out / "pins.json").read_text()) != pins:
            raise ValueError("Existing bundle pins differ")
        manifest = json.loads((out / "manifest.json").read_text())
        if set(manifest) != {*pins["installed"], "libcassini_whistle_cpu.so"}:
            raise ValueError("Existing bundle manifest differs")
        for name, expected in pins["installed"].items():
            if manifest[name] != expected:
                raise ValueError("Existing bundle hashes differ")
        for name, expected in manifest.items():
            verify(out / name, expected)
        for name in ("cpu_limit.c", "NOTICE.md", "LICENSE-Apache-2.0.txt"):
            if (out / name).read_bytes() != (HERE / name).read_bytes():
                raise ValueError(f"Existing bundle source/license differs: {name}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as stage:
        stage = Path(stage)
        for name in ("whistle.cact", "wrapper.whl"):
            shutil.copyfile(cache / name, stage / name)
        with zipfile.ZipFile(cache / "engine.whl") as archive:
            (stage / "libneedle3.so").write_bytes(archive.read("needle/libneedle3.so"))
        for name, expected in pins["installed"].items():
            verify(stage / name, expected)
        for name in ("pins.json", "NOTICE.md", "LICENSE-Apache-2.0.txt"):
            shutil.copyfile(HERE / name, stage / name)
        subprocess.run(
            shlex.split(os.environ.get("CC", "cc"))
            + [
                "-shared",
                "-fPIC",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(HERE / "cpu_limit.c"),
                "-o",
                str(stage / "libcassini_whistle_cpu.so"),
                "-ldl",
            ],
            check=True,
        )
        manifest = dict(pins["installed"])
        manifest["libcassini_whistle_cpu.so"] = hashlib.sha256(
            (stage / "libcassini_whistle_cpu.so").read_bytes()
        ).hexdigest()
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.copyfile(HERE / "cpu_limit.c", stage / "cpu_limit.c")
        stage.rename(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    fetch(args.out, args.cache)
