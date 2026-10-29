# SPDX-License-Identifier: GPL-3.0-or-later
"""D27 owns files by three independent fields, never extension origins."""

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cassini_viewer import companion

OLD = "com.codemyriad.cassini"


def write_legacy(directory, name=OLD, command="cassini-browser-host", **changes):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".json")
    value = dict(name=name, path="/old/location/" + command, allowed_origins=["chrome-extension://different/"])
    value.update(changes)
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize(
    "name,command",
    [
        (OLD, "cassini-browser-host"),
        (OLD, "native_host.py"),
        (OLD, OLD + ".py"),
        (OLD + ".control", "cassini-browser-control-host"),
        (OLD + ".control", "control_host.py"),
        (OLD + ".control", OLD + ".control.py"),
    ],
)
def test_only_owned_legacy_files_are_removed_independent_of_origin(tmp_path, name, command):
    path = write_legacy(tmp_path, name, command)
    assert companion.cleanup_legacy_directory(tmp_path) == [path]
    assert not path.exists() and companion.cleanup_legacy_directory(tmp_path) == []


@pytest.mark.parametrize("case", ["name", "path", "filename", "invalid", "duplicate", "large", "symlink", "fifo"])
def test_foreign_ambiguous_and_nonregular_files_are_preserved(tmp_path, case):
    path = write_legacy(tmp_path)
    value = json.loads(path.read_text())
    if case == "name":
        value["name"] = "another.company"
    elif case == "path":
        value["path"] = "/other/host"
    elif case == "filename":
        path = path.rename(tmp_path / "other.json")
    if case in ("name", "path"):
        value["allowed_origins"] = [companion.ORIGIN]  # Sharing an origin grants no ownership.
        path.write_text(json.dumps(value))
    elif case == "invalid":
        path.write_text("{")
    elif case == "duplicate":
        path.write_text('{"name":"foreign",' + json.dumps(value)[1:])
    elif case == "large":
        path.write_text(json.dumps(value) + " " * 65536)
    elif case in ("symlink", "fifo"):
        path.unlink()
        if case == "symlink":
            target = write_legacy(tmp_path / "elsewhere")
            path.symlink_to(target)
        else:
            os.mkfifo(path)
    before = path.lstat()
    body = path.read_bytes() if stat.S_ISREG(before.st_mode) else None
    assert companion.cleanup_legacy_directory(tmp_path) == []
    after = path.lstat()
    for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns"):
        assert getattr(after, field) == getattr(before, field)
    if body is not None:
        assert path.read_bytes() == body
    if case == "symlink":
        assert path.is_symlink() and target.read_text() == json.dumps(value)


def test_startup_inventory_includes_profiles_of_uninstalled_browsers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(companion, "in_flatpak", lambda: False)
    expected = [write_legacy(tmp_path / folder / "NativeMessagingHosts") for folder in companion.CONFIG_DIRS]
    assert companion.cleanup_legacy() == expected
    assert all(not p.exists() for p in expected)


def test_connect_publishes_only_io_hosts_then_removes_own_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(companion, "in_flatpak", lambda: False)
    browser = companion.Browser("Chromium", "/bin/chromium", tmp_path)
    directory = tmp_path / "NativeMessagingHosts"
    old = [write_legacy(directory), write_legacy(directory, OLD + ".control", "cassini-browser-control-host")]
    executable = tmp_path / "cassini-browser-host"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    paths = companion.register(
        browsers=[browser],
        hosts={
            "io.codemyriad.cassini": executable,
            "io.codemyriad.cassini.control": executable,
        },
    )
    assert {p.name for p in paths} == {"io.codemyriad.cassini.json", "io.codemyriad.cassini.control.json"}
    assert not any(p.exists() for p in old)


def test_standalone_installer_uses_shared_cleanup_and_new_names(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config"
    directory = config / "chromium/NativeMessagingHosts"
    old = write_legacy(directory)
    foreign = write_legacy(directory, OLD + ".control", "other-host")
    before = foreign.read_bytes()
    host = tmp_path / "bin/cassini-browser-host"
    host.parent.mkdir()
    for path in [host, host.with_name("cassini-browser-control-host")]:
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(config))
    subprocess.run(
        [sys.executable, str(root / "browser-extension/install-host.py"), companion.EXTENSION_ID, "--host", str(host)],
        env=env,
        check=True,
        capture_output=True,
    )
    assert not old.exists() and foreign.read_bytes() == before
    assert sorted(p.name for p in directory.glob("io.*")) == [
        "io.codemyriad.cassini.control.json",
        "io.codemyriad.cassini.json",
    ]


def test_startup_cleanup_runs_off_main_thread_and_does_not_keep_quit_alive(monkeypatch):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    threads = []

    def blocked():
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(2)
        done.set()

    monkeypatch.setattr(companion, "cleanup_legacy", blocked)
    start = time.monotonic()
    try:
        companion.start_legacy_cleanup()
        assert time.monotonic() - start < 0.5
        assert entered.wait(1) and threads[0].daemon and threads[0] is not threading.current_thread()
    finally:
        release.set()
        assert done.wait(1)
