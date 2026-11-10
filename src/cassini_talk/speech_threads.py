# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""D92 recognition defaults from the total logical CPU count."""

import logging
import os


def recognition_threads():
    return min(8, max(2, (os.cpu_count() or 1) - 2))


def background_worker_priority():
    """Lower only the recognition child before native pools inherit its priority."""
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 5:
            os.setpriority(os.PRIO_PROCESS, 0, 5)
    except OSError:
        # A scheduling preference must not turn a valid transcription into an error.
        logging.getLogger(__name__).debug("Recognition worker priority is unavailable", exc_info=True)
