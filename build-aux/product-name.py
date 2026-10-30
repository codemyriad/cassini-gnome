#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the coordinator's nontranslated product name for desktop metadata."""

import json
import sys
from pathlib import Path

catalog = json.loads(Path(sys.argv[1]).read_text())
entries = catalog.get("entries", catalog.get("strings", []))
name = next(entry["en"] for entry in entries if entry["id"] == "app.name")
if not name or any(char in name for char in "\r\n\x00"):
    raise SystemExit("Invalid app.name")
print(name)
