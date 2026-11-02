# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in live slow-test evidence, retained even when Meson's total budget expires."""

import os
from types import SimpleNamespace

import pytest


class _Durations:
    def __init__(self, config):
        self.config = config

    def pytest_runtest_logreport(self, report):
        if report.duration < 0.5:
            return
        terminal = self.config.pluginmanager.getplugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"Test elapsed: {report.duration:.3f}s {report.when} {report.nodeid}")
            terminal.flush()


def pytest_configure(config):
    if os.environ.get("CASSINI_TEST_TIMINGS") == "1":
        config.pluginmanager.register(_Durations(config), "cassini-live-durations")


@pytest.fixture(autouse=True)
def ample_scratch_space(monkeypatch):
    """G14: no test depends on the real temp volume's free space.

    Tests of the disk budget pass their own statvfs, or replace this again.
    """
    from cassini_talk import scratch_budget

    ample = SimpleNamespace(f_bavail=1 << 50, f_frsize=1, f_blocks=1 << 51)
    monkeypatch.setattr(scratch_budget, "volume_usage", lambda _path: ample)
