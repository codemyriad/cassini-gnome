#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acquire only D24's pinned golden-test weights through the real D4 store."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cassini_viewer.model_download import ModelStore  # noqa: E402
from cassini_viewer.nemotron_model import FILES, MODEL_ID, REVISION, check_manifest  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cache", type=Path, required=True)
args = parser.parse_args()
store = ModelStore(args.cache)
expected = {name: (size, digest) for name, (size, digest, _compressed) in FILES.items()}
if not store.installed(MODEL_ID, REVISION, expected_files=expected):
    manifest = store.fetch_manifest()
    check_manifest(manifest.model(MODEL_ID, REVISION))
    store.download(MODEL_ID, REVISION, manifest=manifest)
assert store.installed(MODEL_ID, REVISION, expected_files=expected)
print(store.root / MODEL_ID / REVISION)
