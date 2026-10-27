#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Authenticate the vendored Git tree and extension against PINNED_COMMIT, offline."""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "browser-protocol"
METADATA = {"PINNED_COMMIT", "UPSTREAM_COMMIT"}


def object_id(kind, data):
    return hashlib.sha1(kind.encode() + b" " + str(len(data)).encode() + b"\0" + data).digest()


def tree(directory):
    entries = []
    for path in directory.iterdir():
        if path.name == "__pycache__" or (directory == VENDOR and path.name in METADATA):
            continue
        if path.is_symlink():
            raise ValueError(f"Unexpected symlink: {path}")
        if path.is_dir():
            mode, digest, key = b"40000", tree(path), path.name.encode() + b"/"
        else:
            mode = b"100755" if path.stat().st_mode & 0o111 else b"100644"
            digest, key = object_id("blob", path.read_bytes()), path.name.encode()
        entries.append((key, mode + b" " + path.name.encode() + b"\0" + digest))
    return object_id("tree", b"".join(value for _, value in sorted(entries)))


def main():
    commit = (VENDOR / "UPSTREAM_COMMIT").read_bytes()
    pinned = (VENDOR / "PINNED_COMMIT").read_text().strip()
    if object_id("commit", commit).hex() != pinned:
        raise ValueError("PINNED_COMMIT does not identify UPSTREAM_COMMIT")
    expected = commit.splitlines()[0].decode().removeprefix("tree ")
    if tree(VENDOR).hex() != expected:
        raise ValueError(f"Vendored Git tree differs from upstream commit {pinned}")
    subprocess.run([sys.executable, str(VENDOR / "verify.py"), str(ROOT / "browser-extension")], check=True)
    print(f"Verified upstream commit {pinned}")


if __name__ == "__main__":
    main()
