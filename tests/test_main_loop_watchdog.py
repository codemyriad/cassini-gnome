# SPDX-License-Identifier: GPL-3.0-or-later
"""A real main context stalls once, dumps once, and reports recovery."""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("gi", reason="The watchdog exercises the real GLib main context")
SOURCE = Path(__file__).resolve().parents[1] / "src"


def test_stall_dumps_all_threads_once_and_recovers():
    script = """
import logging
import threading
from types import SimpleNamespace

from gi.repository import GLib
from cassini_talk import main_loop_watchdog

loop = GLib.MainLoop()
clock_changed = threading.Condition()
now = 0.0
checks_while_blocked = 0
checked = []

def monotonic():
    global checks_while_blocked
    with clock_changed:
        if now == 0.3:
            checks_while_blocked += 1
            clock_changed.notify_all()
        return now

main_loop_watchdog.time = SimpleNamespace(monotonic=monotonic)

def block():
    global now
    with clock_changed:
        now = 0.3
        # One check reports the stall. The next check must suppress a second
        # report. Entering the third check proves the second one has returned.
        checked.append(clock_changed.wait_for(lambda: checks_while_blocked >= 3, 5))
        now = 0.31
    if not checked[0]:
        loop.quit()
    return GLib.SOURCE_REMOVE

class Watchdog(main_loop_watchdog.MainLoopWatchdog):
    first_beat = True

    def _beat(self):
        keep = super()._beat()
        if self.first_beat:
            self.first_beat = False
            GLib.idle_add(block)
        return keep

class StopAfterRecovery(logging.Handler):
    def emit(self, record):
        if record.getMessage().startswith("main loop recovered"):
            GLib.idle_add(loop.quit)

logging.basicConfig(level=logging.WARNING)
main_loop_watchdog.LOG.addHandler(StopAfterRecovery())
watchdog = Watchdog()
try:
    loop.run()
    assert checked == [True], "watchdog did not check the blocked main context"
finally:
    watchdog.close()
    watchdog._thread.join(2)
    assert not watchdog._thread.is_alive()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15, cwd=SOURCE)
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
from cassini_talk.main_loop_watchdog import MainLoopWatchdog
watchdog = MainLoopWatchdog()
os.kill(os.getpid(), signal.SIGUSR1)
watchdog.close()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5, cwd=SOURCE)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Current thread") == 1, result.stderr
    assert "main loop stalled" not in result.stderr


def test_unarmed_watchdog_ignores_cold_startup_work_supports_sigusr1_and_detects_post_arm_stall():
    script = """
import logging
import os
import signal
import threading
from types import SimpleNamespace

from gi.repository import GLib
from cassini_talk import main_loop_watchdog

loop = GLib.MainLoop()
clock_changed = threading.Condition()
now = 0.0
checks_while_blocked = 0
checked = []

def monotonic():
    global checks_while_blocked
    with clock_changed:
        if now == 0.8:
            checks_while_blocked += 1
            clock_changed.notify_all()
        return now

main_loop_watchdog.time = SimpleNamespace(monotonic=monotonic)

class Watchdog(main_loop_watchdog.MainLoopWatchdog):
    first_beat = True

    def _beat(self):
        keep = super()._beat()
        if self.first_beat:
            self.first_beat = False
            GLib.idle_add(block)
        return keep

def block():
    global now
    with clock_changed:
        now = 0.8
        checked.append(clock_changed.wait_for(lambda: checks_while_blocked >= 2, 5))
        now = 0.85
    if not checked[0]:
        loop.quit()
    return GLib.SOURCE_REMOVE

class StopAfterRecovery(logging.Handler):
    def emit(self, record):
        if record.getMessage().startswith("main loop recovered"):
            GLib.idle_add(loop.quit)

logging.basicConfig(level=logging.WARNING)
main_loop_watchdog.LOG.addHandler(StopAfterRecovery())
watchdog = Watchdog(armed=False)
try:
    # Cold-start work before arm() never triggers a stall traceback, while SIGUSR1 still works.
    assert watchdog._thread is None and watchdog._source == 0
    now = 0.45
    os.kill(os.getpid(), signal.SIGUSR1)
    assert watchdog.arm() is True
    assert watchdog.arm() is False
    loop.run()
    assert checked == [True], "armed watchdog did not check post-presentation stall"
finally:
    watchdog.close()
    watchdog._thread.join(2)
    assert not watchdog._thread.is_alive()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15, cwd=SOURCE)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("main loop stalled") == 1, result.stderr
    # One traceback from explicit SIGUSR1 while unarmed + one from the post-arm stall.
    assert result.stderr.count("Current thread") == 2, result.stderr
    assert result.stderr.count("main loop recovered") == 1, result.stderr
