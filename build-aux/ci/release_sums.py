#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Collect a release's five native packages and write their SHA256SUMS.

A release attaches exactly two .debs (Ubuntu 26.04 and Debian testing), two
x86_64 RPMs (Fedora and openSUSE) and one Arch package, plus SHA256SUMS in
sha256sum's own format, one line per package by file name, so downloaders
run `sha256sum -c SHA256SUMS` next to what they fetched. Prints the files to
attach, one per line, the checksum file last.
"""

import argparse
import hashlib
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
    args = parser.parse_args()
    try:
        files = packages(args.directory)
    except ValueError as error:
        parser.error(str(error))
    write_sums(files, args.sums)
    print("\n".join(str(p) for p in [*files, args.sums]))


if __name__ == "__main__":
    main()
