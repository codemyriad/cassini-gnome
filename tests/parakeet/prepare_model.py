#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acquire the immutable D16 golden fixture through D4, before networkless tests."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cassini_talk.model_download import ModelStore  # noqa: E402
from cassini_talk.parakeet_model import MODEL_ID, REVISION, check_manifest, model_path, verify_model  # noqa: E402

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
    print("D16 fixture cache miss; acquiring pinned test weights (90 s socket timeout)", flush=True)
    # Hosted runners can spend over ten seconds in a dist TLS handshake. This
    # is build-fixture acquisition only; the app's cancellable D4 timeout stays
    # unchanged, and hash failures still fail without retry.
    store = ModelStore(args.cache, timeout=90, user_agent="CassiniTalk/ci-fetch (GNOME; Linux; source)")
    manifest = store.fetch_manifest()
    check_manifest(manifest.model(MODEL_ID, REVISION))
    last = [0.0, None]

    def progress(phase, key, count, total):
        now = time.monotonic()
        if phase != last[1] or now - last[0] >= 5:
            print(f"D16 fixture {phase}: {key} {count}/{total} bytes", flush=True)
            last[:] = [now, phase]

    store.download(MODEL_ID, REVISION, manifest=manifest, progress=progress)
    verify_model(path)
print(path)
