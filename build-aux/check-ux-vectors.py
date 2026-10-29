#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check frozen vector bytes; --source compares them with coordinator ux-draft/."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def catalog_additions(data, adoption):
    """Verify selective published additions without changing any older copy."""
    value = json.loads(data)
    ids = set(adoption["ids"])
    entries = [e for e in value["entries"] if e["id"] in ids]
    if len(entries) != len(ids) or {e["id"] for e in entries} != ids:
        raise ValueError("Missing or duplicate catalog additions")
    selected = json.dumps(entries, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    if hashlib.sha256(selected).hexdigest() != adoption["entriesSha256"]:
        raise ValueError("Catalog additions differ from approved published entries")
    value["entries"] = [e for e in value["entries"] if e["id"] not in ids]
    base = (json.dumps(value, indent=1, ensure_ascii=False) + "\n").encode()
    if hashlib.sha256(base).hexdigest() != adoption["baseSha256"]:
        raise ValueError("Older catalog bytes changed during selective adoption")
    return entries


def check(source=None):
    copies = ROOT / "tests/ux"
    provenance = json.loads((copies / "UPSTREAM.json").read_text())
    for name, expected in provenance["sha256"].items():
        data = (ROOT / provenance.get("paths", {}).get(name, "tests/ux/" + name)).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Golden copy changed: {name}")
        adoption = provenance.get("additionalSources", {}).get(name, {}).get("additive")
        entries = catalog_additions(data, adoption) if adoption else None
        if source is not None:
            published = (Path(source) / name).read_bytes()
            if adoption:
                ids = set(adoption["ids"])
                same = entries == [e for e in json.loads(published)["entries"] if e["id"] in ids]
            else:
                same = data == published
            if not same:
                raise ValueError(f"Golden copy differs from coordinator: {name}")
    print(f"Verified {len(provenance['sha256'])} unchanged UX vector files at {provenance['commit']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    check(parser.parse_args().source)
