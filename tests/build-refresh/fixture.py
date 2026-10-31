#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Create a test-only same-version .deb patch; never upload these fixtures."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def patch_tree(root, marker):
    root = Path(root)
    module = root / "cassini_talk/main.py"
    with module.open("a") as stream:
        stream.write(f'\nprint("CASSINI_TEST_IMAGE={marker}", file=sys.stderr, flush=True)\n')
    path = root / "build-info.json"
    value = json.loads(path.read_text())
    value["files"]["cassini_talk/main.py"] = hashlib.sha256(module.read_bytes()).hexdigest()
    del value["buildId"]
    value["buildId"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    return value


def package(original, output, marker):
    temporary = output.parent / ("tree-" + marker)
    subprocess.run(["dpkg-deb", "--raw-extract", str(original), str(temporary)], check=True)
    value = patch_tree(temporary / "usr/share/cassini-talk", marker)
    sums = temporary / "DEBIAN/md5sums"
    if sums.exists():
        lines = []
        for line in sums.read_text().splitlines():
            _, name = line.split(None, 1)
            name = name.strip()
            digest = hashlib.md5((temporary / name).read_bytes(), usedforsecurity=False).hexdigest()
            lines.append(f"{digest}  {name}\n")
        sums.write_text("".join(lines))
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(temporary), str(output)], check=True)
    shutil.rmtree(temporary)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("marker")
    args = parser.parse_args()
    print(json.dumps(package(args.original, args.output, args.marker)))
