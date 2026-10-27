#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check frozen vector bytes; --source compares them with coordinator ux-draft/."""

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    check(parser.parse_args().source)
