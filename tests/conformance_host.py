#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated host for the upstream stdio runner; failed artifacts are retained."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cassini_talk.browser_host import main  # noqa: E402

directory = os.environ.get("CASSINI_CONFORMANCE_DIR", str(Path(tempfile.gettempdir()) / "cassini-conformance"))
sys.exit(main(directory=directory, open_completed=False))
