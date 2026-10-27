#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Development launcher; packaged installations use cassini-browser-host."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cassini_viewer.browser_host import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
