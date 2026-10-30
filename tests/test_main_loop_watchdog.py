# SPDX-License-Identifier: GPL-3.0-or-later
"""A real main context stalls once, dumps once, and reports recovery."""

import subprocess
import sys


def test_stall_dumps_all_threads_once_and_recovers():
    script = """
import logging, time
from gi.repository import GLib
from cassini_viewer.main_loop_watchdog import MainLoopWatchdog
logging.basicConfig(level=logging.WARNING)
watchdog = MainLoopWatchdog()
loop = GLib.MainLoop()
def block():
    time.sleep(0.4)
    return False
GLib.timeout_add(120, block)
GLib.timeout_add(800, lambda: (loop.quit(), False)[1])
loop.run()
watchdog.close()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("main loop stalled") == 1, result.stderr
    assert result.stderr.count("Current thread") == 1, result.stderr
    assert result.stderr.count("main loop recovered") == 1, result.stderr
    assert "major-fault delta:" in result.stderr
    assert "in block" in result.stderr


def test_sigusr1_dumps_without_waiting_for_a_stall():
    script = """
import os, signal
from gi.repository import GLib
from cassini_viewer.main_loop_watchdog import MainLoopWatchdog
watchdog = MainLoopWatchdog()
os.kill(os.getpid(), signal.SIGUSR1)
watchdog.close()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Current thread") == 1, result.stderr
    assert "main loop stalled" not in result.stderr
