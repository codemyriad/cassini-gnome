#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the real pinned portal as a CI host tool in the package-manager-free SDK."""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "1.22.1"
SHA256 = "d4879ddb3d65ff1a8f19187497e6f13dc5d267bcac404a5d501218be355753d3"
URL = f"https://github.com/flatpak/xdg-desktop-portal/releases/download/{VERSION}/xdg-desktop-portal-{VERSION}.tar.xz"


def prepare(output, cache):
    output = output.resolve()
    identity = dict(version=VERSION, sha256=SHA256, prefix=str(output))
    marker = output / "cassini-test-tool.json"
    if (
        marker.exists()
        and json.loads(marker.read_text()) == identity
        and (output / "libexec/xdg-desktop-portal").is_file()
    ):
        return
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = cache / f"xdg-desktop-portal-{VERSION}.tar.xz"
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as target:
            temporary = Path(target.name)
            try:
                with urllib.request.urlopen(URL, timeout=30) as incoming:
                    while block := incoming.read(1024 * 1024):
                        target.write(block)
                target.flush()
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256:
                    raise ValueError("Portal source SHA256 mismatch")
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("Cached portal source SHA256 mismatch")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cassini-portal-build-") as temporary:
        root = Path(temporary)
        with tarfile.open(archive) as source:
            source.extractall(root, filter="data")
        build = root / "build"
        subprocess.run(
            [
                "meson",
                "setup",
                str(build),
                str(root / f"xdg-desktop-portal-{VERSION}"),
                "--prefix=" + str(output),
                "--libexecdir=libexec",
                "--buildtype=release",
                "-Dtests=disabled",
                "-Ddocumentation=disabled",
                "-Dman-pages=disabled",
                "-Dflatpak-interfaces=disabled",
                "-Dgeoclue=disabled",
                "-Dgudev=disabled",
                "-Dsystemd=disabled",
            ],
            check=True,
        )
        subprocess.run(["meson", "compile", "-C", str(build), "-j", "2"], check=True)
        subprocess.run(["meson", "install", "-C", str(build), "--no-rebuild"], check=True)
    marker.write_text(json.dumps(identity, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassini/portal-test-tool",
    )
    args = parser.parse_args()
    prepare(args.output, args.cache)


if __name__ == "__main__":
    main()
