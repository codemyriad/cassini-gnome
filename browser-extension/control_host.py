#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Development management host; installed builds use cassini-browser-control-host."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cassini_talk.browser_control_host import main

sys.exit(main())
