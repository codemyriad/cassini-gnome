#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Refuse to release a Flatpak bundle without the signed update remote (G15, D45.1).

Main builds the stable bundle without --repo-url until the D45.1 repository
is published, so the release reuses exactly the bundle main tested. A bundle
installed without an origin gets no updates, so the tag workflow stops here
until main's bundle names the signed repository and carries its key."""

import argparse
import sys
from pathlib import Path

URL = "https://cassini.talk/download/linux/flatpak/"
# The static-delta superblock `flatpak build-bundle` writes; its first member
# holds the bundle metadata (origin, runtime-repo, gpg-keys, metadata, ...).
SUPERBLOCK = "(a{sv}tayay(a(uayttay)a(yaytt)))"


def metadata(path):
    from gi.repository import GLib

    data = GLib.Bytes.new(Path(path).read_bytes())
    value = GLib.Variant.new_from_bytes(GLib.VariantType.new(SUPERBLOCK), data, False)
    return value.get_child_value(0).unpack()


def problems(values):
    found = []
    if values.get("origin") != URL:
        found.append(f"no update remote: origin is {values.get('origin')!r}, not {URL!r}")
    if not values.get("gpg-keys"):
        found.append("no gpg-keys for the update remote")
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    args = parser.parse_args()
    found = problems(metadata(args.bundle))
    for problem in found:
        print(f"::error::{args.bundle}: {problem} (G15: release only after D45.1 is published)")
    if found:
        return 1
    print(f"{args.bundle}: update remote {URL} with its signing key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
