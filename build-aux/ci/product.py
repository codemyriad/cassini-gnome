#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve executable modes and verify exact-commit/profile CI build products."""

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Unexpected symbolic link in build product")
        if path.is_file():
            with path.open("rb") as stream:
                result[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def pack(root, archive, commit, profile):
    manifest = dict(version=1, commit=commit, profile=profile, files=inventory(root))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        with tarfile.open(archive, "w") as output:
            output.add(path, arcname="manifest.json")
            output.add(root, arcname="product")


def unpack(archive, destination, commit, profile):
    if destination.exists():
        raise ValueError("Build product destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        root = Path(tmp)
        with tarfile.open(archive) as source:
            # Only regular files/directories under these two roots; no links,
            # devices or path traversal. This also works before Python 3.14.
            for member in source.getmembers():
                parts = Path(member.name).parts
                if (
                    not parts
                    or parts[0] not in ("product", "manifest.json")
                    or ".." in parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("Unsafe build product archive")
            source.extractall(root, filter="data")
        manifest = json.loads((root / "manifest.json").read_text())
        if (manifest.get("version"), manifest.get("commit"), manifest.get("profile")) != (1, commit, profile):
            raise ValueError("Build product commit/profile mismatch")
        if inventory(root / "product") != manifest["files"]:
            raise ValueError("Build product digest mismatch")
        shutil.move(root / "product", destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "unpack"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--profile", choices=("core", "boards", "offline"), required=True)
    args = parser.parse_args()
    function = (
        pack if args.action == "pack" else lambda root, archive, commit, profile: unpack(archive, root, commit, profile)
    )
    function(args.directory, args.archive, args.commit, args.profile)
    print(f"Verified {args.profile} build product for {args.commit}")


if __name__ == "__main__":
    main()
