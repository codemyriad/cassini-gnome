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
FUSE_VERSION = "3.18.3"
FUSE_SHA256 = "bcd19582c5e30f7fe45dd86a5540e998590aa01903afc7ebcbeea6c8ac5421ee"
FUSE_URL = f"https://github.com/libfuse/libfuse/releases/download/fuse-{FUSE_VERSION}/fuse-{FUSE_VERSION}.tar.gz"


def fetch(cache, name, url, digest):
    archive = cache / name
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as target:
            temporary = Path(target.name)
            try:
                with urllib.request.urlopen(url, timeout=30) as incoming:
                    while block := incoming.read(1024 * 1024):
                        target.write(block)
                target.flush()
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise ValueError("Test-tool source SHA256 mismatch")
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError("Cached test-tool source SHA256 mismatch")
    return archive


def prepare(output, cache):
    output = output.resolve()
    identity = dict(version=VERSION, sha256=SHA256, fuseSHA256=FUSE_SHA256, prefix=str(output))
    marker = output / "cassini-test-tool.json"
    if (
        marker.exists()
        and json.loads(marker.read_text()) == identity
        and (output / "libexec/xdg-desktop-portal").is_file()
    ):
        return
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = fetch(cache, f"xdg-desktop-portal-{VERSION}.tar.xz", URL, SHA256)
    fuse = fetch(cache, f"fuse-{FUSE_VERSION}.tar.gz", FUSE_URL, FUSE_SHA256)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cassini-portal-build-") as temporary:
        root = Path(temporary)
        for packed in (fuse, archive):
            with tarfile.open(packed) as source:
                source.extractall(root, filter="data")
        # The SDK lacks fuse3 development files, and has no distro package manager.
        # Install only a private, unprivileged CI dependency, with no helper/mount use.
        fuse_build = root / "fuse-build"
        subprocess.run(
            [
                "meson",
                "setup",
                str(fuse_build),
                str(root / f"fuse-{FUSE_VERSION}"),
                "--prefix=" + str(output),
                "--libdir=lib",
                "--buildtype=release",
                "-Dutils=false",
                "-Dexamples=false",
                "-Dtests=false",
                "-Duseroot=false",
                "-Dinitscriptdir=",
                "-Denable-io-uring=false",
            ],
            check=True,
        )
        subprocess.run(["meson", "compile", "-C", str(fuse_build), "-j", "2"], check=True)
        subprocess.run(["meson", "install", "-C", str(fuse_build), "--no-rebuild"], check=True)
        build = root / "build"
        environment = dict(os.environ)
        environment["PKG_CONFIG_PATH"] = str(output / "lib/pkgconfig") + ":" + environment.get("PKG_CONFIG_PATH", "")
        subprocess.run(
            [
                "meson",
                "setup",
                str(build),
                str(root / f"xdg-desktop-portal-{VERSION}"),
                "--prefix=" + str(output),
                "--libdir=lib",
                "--libexecdir=libexec",
                "--buildtype=release",
                "-Dc_link_args=-Wl,-rpath," + str(output / "lib"),
                "-Dtests=disabled",
                "-Ddocumentation=disabled",
                "-Dman-pages=disabled",
                "-Dflatpak-interfaces=disabled",
                "-Dgeoclue=disabled",
                "-Dgudev=disabled",
                "-Dsystemd=disabled",
            ],
            env=environment,
            check=True,
        )
        subprocess.run(["meson", "compile", "-C", str(build), "-j", "2"], env=environment, check=True)
        subprocess.run(["meson", "install", "-C", str(build), "--no-rebuild"], env=environment, check=True)
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
