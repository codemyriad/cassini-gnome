#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify the complete bundle before staging it for packaging; never load it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cassini_viewer.whistle_worker import verify_bundle  # noqa: E402

print(verify_bundle(sys.argv[1]))
