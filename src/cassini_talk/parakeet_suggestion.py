# SPDX-License-Identifier: GPL-3.0-or-later
"""D78 eligibility without GTK, engine imports, downloads or preference writes."""

import os
from pathlib import Path

from .sherpa_runtime import available

MINIMUM_RAM = 8_000_000_000


def memory_bytes():
    total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    # A constrained process must not recommend weights its container cannot run.
    try:
        limit = (Path("/sys/fs/cgroup") / "memory.max").read_text().strip()
        if limit != "max":
            total = min(total, int(limit))
    except (OSError, ValueError):
        pass
    return total


def capable():
    return available() and memory_bytes() >= MINIMUM_RAM


def offer(state, dismissed):
    return bool(
        not dismissed
        and state.get("suggestible")
        and not state.get("downloaded")
        and not state.get("busy")
        and not state.get("downloading")
    )
