# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in live slow-test evidence, retained even when Meson's total budget expires,
and the shard selection and per-shard isolation for Meson's pytest-N tests (G3)."""

import fnmatch
import os
import shutil
import tempfile
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import shard_plan

TESTS = Path(__file__).resolve().parent
_shard = pytest.StashKey()


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


def _test_files(config):
    return shard_plan.test_files(TESTS, config.getini("python_files"))


def _isolate(root):
    """Private HOME, XDG base directories and basetemp for one shard."""
    home = root / "home"
    dirs = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_DATA_HOME": home / ".local/share",
        "XDG_RUNTIME_DIR": root / "run",
    }
    display = os.environ.get("WAYLAND_DISPLAY")
    if display and not os.path.isabs(display) and os.environ.get("XDG_RUNTIME_DIR"):
        # Keep a developer's display reachable from the private runtime dir.
        os.environ["WAYLAND_DISPLAY"] = os.path.join(os.environ["XDG_RUNTIME_DIR"], display)
    for name, path in dirs.items():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.environ[name] = str(path)
    return root / "tmp"


def _require_gtk():
    """G11: the GTK application tests skip without these, and a package check
    that CI's Source job relies on must run them, not skip them."""
    try:
        import gi

        for namespace, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
            gi.require_version(namespace, version)
    except (ImportError, ValueError) as error:
        raise pytest.UsageError(f"CASSINI_TEST_REQUIRE_GTK=1 but {error}") from error
    if shutil.which("glib-compile-resources") is None:
        raise pytest.UsageError("CASSINI_TEST_REQUIRE_GTK=1 but glib-compile-resources is missing")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if os.environ.get("CASSINI_TEST_REQUIRE_GTK") == "1":
        _require_gtk()
    if os.environ.get("CASSINI_TEST_TIMINGS") == "1":
        config.pluginmanager.register(_Durations(config), "cassini-live-durations")
    if value := os.environ.get("CASSINI_TEST_SHARD"):
        index, count = shard_plan.parse(value)
        assignment = shard_plan.assign(_test_files(config), count)
        root = Path(tempfile.mkdtemp(prefix=f"cassini-pytest-{index}of{count}-"))
        # Before the tmp_path factory reads it (tryfirst).
        config.option.basetemp = str(_isolate(root))
        mine = {name for name, shard in assignment.items() if shard == index}
        config.stash[_shard] = SimpleNamespace(
            index=index, count=count, assignment=assignment, root=root, started=time.monotonic()
        )
        print(f"pytest shard {index}/{count}: {len(mine)} of {len(assignment)} files; HOME, XDG dirs, basetemp: {root}")


def pytest_ignore_collect(collection_path, config):
    shard = config.stash.get(_shard, None)
    if shard is None or not collection_path.is_file():
        return None
    patterns = config.getini("python_files")
    if not any(fnmatch.fnmatch(collection_path.name, pattern) for pattern in patterns):
        return None
    try:
        name = collection_path.resolve().relative_to(TESTS).as_posix()
    except ValueError:  # Reached through a symlinked directory that leads out of tests/.
        name = collection_path.as_posix()
    owner = shard.assignment.get(name)
    if owner is None:
        # A file pytest reaches but _test_files does not list (pytest follows
        # symlinked directories, rglob does not): a fixed shard by name, so it
        # still runs exactly once, never in no shard or in all of them.
        owner = zlib.crc32(name.encode()) % shard.count + 1
    return True if owner != shard.index else None


def pytest_sessionfinish(session, exitstatus):
    shard = session.config.stash.get(_shard, None)
    if shard is None:
        return
    elapsed = time.monotonic() - shard.started
    budget = float(os.environ.get("CASSINI_TEST_BUDGET") or 0)
    terminal = session.config.pluginmanager.getplugin("terminalreporter")
    line = f"pytest shard {shard.index}/{shard.count} took {elapsed:.1f}s"
    if budget and elapsed > budget / 2:
        # A GitHub annotation, not a failure; CI prints these from meson-logs.
        line = (
            f"::warning title=pytest shard over half its budget::pytest-{shard.index} took {elapsed:.0f} s "
            f"of its {budget:.0f} s Meson timeout; rebalance tests/shard-durations.json or add a shard"
        )
    if terminal is not None:
        terminal.write("\n")  # end the progress dots: the line must start with ::warning
        terminal.write_line(line)
    else:
        print(line)
    shard.passed = exitstatus == 0


def pytest_unconfigure(config):
    shard = config.stash.get(_shard, None)
    if shard is not None and getattr(shard, "passed", False):
        shutil.rmtree(shard.root, ignore_errors=True)


@pytest.fixture(autouse=True)
def ample_scratch_space(monkeypatch):
    """G14: no test depends on the real temp volume's free space.

    Tests of the disk budget pass their own statvfs, or replace this again.
    """
    from cassini_talk import scratch_budget

    ample = SimpleNamespace(f_bavail=1 << 50, f_frsize=1, f_blocks=1 << 51)
    monkeypatch.setattr(scratch_budget, "volume_usage", lambda _path: ample)
