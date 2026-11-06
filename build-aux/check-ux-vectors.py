#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check frozen UX bytes, or explicitly refresh them from a coordinator snapshot."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(source=None):
    copies = ROOT / "tests/ux"
    provenance = json.loads((copies / "UPSTREAM.json").read_text())
    for name, expected in provenance["sha256"].items():
        data = (ROOT / provenance.get("paths", {}).get(name, "tests/ux/" + name)).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Golden copy changed: {name}")
        if source is not None and data != (Path(source) / name).read_bytes():
            raise ValueError(f"Golden copy differs from coordinator: {name}")
    print(f"Verified {len(provenance['sha256'])} unchanged UX vector files at {provenance['commit']}")


def refresh(source, commit):
    """Read the complete approved snapshot before changing any pinned bytes."""
    provenance_path = ROOT / "tests/ux/UPSTREAM.json"
    provenance = json.loads(provenance_path.read_text())
    snapshot = {name: (source / name).read_bytes() for name in provenance["sha256"]}
    for name, data in snapshot.items():
        target = ROOT / provenance.get("paths", {}).get(name, "tests/ux/" + name)
        target.write_bytes(data)
    provenance["commit"] = commit
    provenance["sha256"] = {name: hashlib.sha256(data).hexdigest() for name, data in snapshot.items()}
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n")
    check(source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--refresh", action="store_true", help="copy the entire approved source snapshot and refresh hashes"
    )
    parser.add_argument("--commit", help="coordination revision represented by --source (required for --refresh)")
    args = parser.parse_args()
    if args.refresh:
        if args.source is None or not args.commit:
            parser.error("--refresh requires --source and --commit")
        refresh(args.source, args.commit)
    else:
        if args.commit:
            parser.error("--commit requires --refresh")
        check(args.source)
