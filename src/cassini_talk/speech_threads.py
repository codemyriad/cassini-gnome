# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""D92 logical CPU headroom, respecting the process affinity and CPU quota."""

import logging
import os
from pathlib import Path


def _cgroup_cpu_limit(cgroup_root=Path("/sys/fs/cgroup"), proc_cgroup=Path("/proc/self/cgroup")):
    # A private cgroup namespace may expose only its root. Always inspect that
    # root as well as the visible process path and its enclosing quota limits.
    candidates = [cgroup_root]
    try:
        for line in proc_cgroup.read_text().splitlines():
            if line.startswith("0::"):
                root = cgroup_root.resolve()
                current = (root / line[3:].strip().lstrip("/")).resolve()
                if current.is_relative_to(root):
                    while current != root:
                        candidates.append(current)
                        current = current.parent
                break
    except (OSError, ValueError):
        pass
    limits = []
    for directory in candidates:
        try:
            quota_text, period_text = (directory / "cpu.max").read_text().split()
            if quota_text != "max":
                quota, period = int(quota_text), int(period_text)
                if quota > 0 and period > 0:
                    limits.append(max(1, quota // period))
        except (OSError, ValueError):
            pass
    return min(limits) if limits else None


def available_cpus():
    counts = []
    total = os.cpu_count()
    if total and total > 0:
        counts.append(total)
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            count = len(affinity(0))
            if count > 0:
                counts.append(count)
        except OSError:
            pass
    quota = _cgroup_cpu_limit()
    if quota is not None:
        counts.append(quota)
    return min(counts) if counts else 1


def recognition_threads():
    return min(8, max(2, available_cpus() - 2))


def background_worker_priority():
    """Lower only the recognition child before native pools inherit its priority."""
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 5:
            os.setpriority(os.PRIO_PROCESS, 0, 5)
    except OSError:
        # A scheduling preference must not turn a valid transcription into an error.
        logging.getLogger(__name__).debug("Recognition worker priority is unavailable", exc_info=True)
