# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnose GTK stalls without depending on a responsive GTK main thread."""

import faulthandler
import logging
import resource
import signal
import threading
import time

LOG = logging.getLogger(__name__)


class MainLoopWatchdog:
    def __init__(self):
        from gi.repository import GLib

        self._stopped = threading.Event()
        self._heartbeat = None
        self._source = GLib.timeout_add(50, self._beat)
        # SIGUSR1 provides an on-demand dump even before the stall threshold.
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        self._thread = threading.Thread(target=self._watch, name="cassini-stall-watchdog", daemon=True)
        self._thread.start()

    def _beat(self):
        self._heartbeat = (time.monotonic(), resource.getrusage(resource.RUSAGE_SELF).ru_majflt)
        return not self._stopped.is_set()

    def _watch(self):
        reported = None
        reported_faults = 0
        while not self._stopped.wait(0.05):
            heartbeat = self._heartbeat
            if heartbeat is None:
                continue  # The main context has not started dispatching yet.
            stamp, faults = heartbeat
            elapsed = time.monotonic() - stamp
            if reported is not None and stamp != reported:
                LOG.warning(
                    "main loop recovered; major faults since stall: %d",
                    resource.getrusage(resource.RUSAGE_SELF).ru_majflt - reported_faults,
                )
                reported = None
            if elapsed > 0.25 and reported is None:
                reported, reported_faults = stamp, faults
                LOG.warning(
                    "main loop stalled %.0f ms; major-fault delta: %d",
                    elapsed * 1000,
                    resource.getrusage(resource.RUSAGE_SELF).ru_majflt - faults,
                )
                faulthandler.dump_traceback(all_threads=True)

    def close(self):
        from gi.repository import GLib

        self._stopped.set()
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        faulthandler.unregister(signal.SIGUSR1)
