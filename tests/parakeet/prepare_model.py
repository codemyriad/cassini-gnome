#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acquire the immutable D16 golden fixture through D4, before networkless tests."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cassini_viewer.model_download import ModelStore  # noqa: E402
from cassini_viewer.parakeet_model import MODEL_ID, REVISION, check_manifest, model_path, verify_model  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--cache", type=Path, required=True)
parser.add_argument("--offline", action="store_true")
args = parser.parse_args()
path = model_path(args.cache)
try:
    verify_model(path)
except FileNotFoundError:
    if args.offline:
        raise
    store = ModelStore(args.cache)
    manifest = store.fetch_manifest()
    check_manifest(manifest.model(MODEL_ID, REVISION))
    store.download(MODEL_ID, REVISION, manifest=manifest)
    verify_model(path)
print(path)
