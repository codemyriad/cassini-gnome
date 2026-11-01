#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated host for the upstream stdio runner; failed artifacts are retained.

The runner gives every case a private test Library in CASSINI_LIBRARY_DIR and
stops the run if a meeting lands anywhere else, so there is no default.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cassini_talk.browser_host import main  # noqa: E402

sys.exit(main(directory=os.environ["CASSINI_LIBRARY_DIR"], open_completed=False))
