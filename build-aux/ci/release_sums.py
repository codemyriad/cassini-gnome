#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Collect a release's five native packages and write their SHA256SUMS.

A release attaches exactly two .debs (Ubuntu 26.04 and Debian testing), two
x86_64 RPMs (Fedora and openSUSE) and one Arch package, plus SHA256SUMS in
sha256sum's own format, one line per package by file name, so downloaders
run `sha256sum -c SHA256SUMS` next to what they fetched.

GitHub renames an uploaded asset whose name has other characters than
letters, digits and `._-` (v0.4.0's `cassini-talk_0.4.0~ubuntu26.04_amd64.deb`
was published as `cassini-talk_0.4.0.ubuntu26.04_amd64.deb`, so its SHA256SUMS
line named a file the release did not have). So every package is staged under
the name GitHub publishes, and SHA256SUMS names those. Prints the staged files
to attach, one per line, the checksum file last.
"""

import argparse
import hashlib
import re
import shutil
from pathlib import Path

KINDS = {"deb": ("*.deb", 2), "rpm": ("*.x86_64.rpm", 2), "arch": ("*.pkg.tar.zst", 1)}


def packages(directory):
    """The five packages under `directory`, refusing a missing, extra or same-named one."""
    found = []
    for kind, (pattern, count) in KINDS.items():
        matches = sorted(p for p in Path(directory).rglob(pattern) if p.is_file())
        if len(matches) != count:
            raise ValueError(f"Expected {count} {kind} package(s), found {[p.name for p in matches]}")
        found += matches
    names = [p.name for p in found]
    if len(set(names)) != len(names):
        raise ValueError(f"Two packages share a file name: {names}")
    return found


def asset_name(name):
    """The name GitHub gives an uploaded asset: other characters than letters, digits and ._- become '.'."""
    return re.sub(r"[^A-Za-z0-9._-]", ".", name)


def stage(files, directory):
    """Copies of `files` in `directory`, each under its published name; two that would collide fail."""
    directory.mkdir(parents=True, exist_ok=True)
    names = [asset_name(p.name) for p in files]
    if len(set(names)) != len(names):
        raise ValueError(f"Two packages share a published name: {names}")
    staged = []
    for path, name in zip(files, names, strict=True):
        shutil.copyfile(path, directory / name)
        staged.append(directory / name)
    return staged


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_sums(files, output):
    """SHA256SUMS lines as `sha256sum` writes them (two spaces, then the file name)."""
    output.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="where the downloaded release artifacts are")
    parser.add_argument("--sums", type=Path, required=True, help="the SHA256SUMS file to write")
    parser.add_argument(
        "--stage", type=Path, required=True, help="where to copy the packages under their published names"
    )
    args = parser.parse_args()
    try:
        files = stage(packages(args.directory), args.stage)
    except ValueError as error:
        parser.error(str(error))
    write_sums(files, args.sums)
    print("\n".join(str(p) for p in [*files, args.sums]))


if __name__ == "__main__":
    main()
