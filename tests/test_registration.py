# SPDX-License-Identifier: GPL-3.0-or-later
"""D42 health, safe ownership, actual Flatpak relays and atomic repair races."""

import json
import logging
import subprocess
from pathlib import Path

import pytest
from test_browser_connection import Clock
from test_flatpak_bridge import host as host_fixture

from cassini_talk import companion, registration
from cassini_talk import flatpak_bridge as bridge
from cassini_talk.browser_connection import setup

host = host_fixture


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def hosts(path):
    return dict.fromkeys(("io.codemyriad.cassini", "io.codemyriad.cassini.control"), path)


def test_source_keeps_valid_foreign_release_until_explicit_setup(tmp_path):
    browser = companion.Browser("Chromium", "/test/browser", tmp_path / "profile")
    release = executable(tmp_path / "release/host")
    source = executable(tmp_path / "source/host")
    paths = companion.register(browsers=[browser], hosts=hosts(release), force=True)
    before = {p: (p.read_bytes(), p.stat().st_ino) for p in paths}
    companion.register(browsers=[browser], hosts=hosts(source))
    assert {p: (p.read_bytes(), p.stat().st_ino) for p in paths} == before
    companion.register(browsers=[browser], hosts=hosts(source), force=True)
    assert all(json.loads(p.read_text())["path"] == str(source) for p in paths)


def test_dead_executable_is_healed_and_logged_once(tmp_path, caplog):
    browser = companion.Browser("Chromium", "/test/browser", tmp_path / "profile")
    dead = executable(tmp_path / "dead")
    live = executable(tmp_path / "live")
    paths = companion.register(browsers=[browser], hosts=hosts(dead), force=True)
    dead.unlink()
    assert registration.health([browser])
    with caplog.at_level(logging.INFO):
        companion.register(browsers=[browser], hosts=hosts(live))
        companion.register(browsers=[browser], hosts=hosts(live))
    assert not registration.health([browser])
    assert sum("Repaired a dead" in record.message for record in caplog.records) == 2  # one per host
    assert all(json.loads(p.read_text())["path"] == str(live) for p in paths)


def test_nonexecutable_is_a_dead_target_and_own_field_changes_are_repaired(tmp_path):
    browser = companion.Browser("Chromium", "/test/browser", tmp_path / "profile")
    current = executable(tmp_path / "host")
    paths = companion.register(browsers=[browser], hosts=hosts(current))
    value = json.loads(paths[0].read_text())
    value["allowed_origins"] = ["chrome-extension://wrong/"]
    paths[0].write_text(json.dumps(value))
    companion.register(browsers=[browser], hosts=hosts(current))
    assert json.loads(paths[0].read_text())["allowed_origins"] == [companion.ORIGIN]
    current.chmod(0o600)
    assert registration.health([browser])


def test_executable_relay_for_removed_flatpak_is_healed_by_native_build(host, monkeypatch):
    paths = bridge.register()
    browser = bridge.installed_browsers()[0]
    assert registration.native_target(json.loads(paths[0].read_text())["path"])
    (host / "deployment").rename(host / "uninstalled-deployment")
    relay = Path(json.loads(paths[0].read_text())["path"])
    assert relay.is_file() and relay.stat().st_mode & 0o100  # file itself still looks runnable
    assert not registration.native_target(relay)
    live = executable(host / "new-native/host")
    monkeypatch.setattr(companion, "in_flatpak", lambda: False)
    companion.register(browsers=[browser], hosts=hosts(live))
    assert all(json.loads(p.read_text())["path"] == str(live) for p in paths[:2])


def test_unknown_flatpak_inventory_error_does_not_discard_valid_foreign_registration(host, monkeypatch):
    paths = bridge.register()
    before = paths[0].read_bytes()

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("flatpak", 3)

    with pytest.raises(OSError):
        registration.installed_relay(
            ("io.codemyriad.CassiniTalk.Devel", "x86_64", "master", "cassini-browser-host"),
            run=unavailable,
            flatpak="/flatpak",
        )
    monkeypatch.setattr(registration, "native_target", lambda _path: (_ for _ in ()).throw(OSError("unknown")))
    browser = bridge.installed_browsers()[0]
    with pytest.raises(OSError):
        companion.register(browsers=[browser], hosts=hosts(executable(host / "native")))
    assert paths[0].read_bytes() == before


def test_flatpak_devel_preserves_valid_native_and_explicit_setup_can_own_it(host):
    browser = bridge.installed_browsers()[0]
    paths = companion.register(browsers=[browser], hosts=hosts(executable(host / "release-native")))
    original = {p: p.read_bytes() for p in paths}
    bridge.register([browser])
    assert {p: p.read_bytes() for p in paths} == original
    bridge.register([browser], force=True)
    assert all("native-hosts" in json.loads(p.read_text())["path"] for p in paths)


def test_host_compare_and_swap_preserves_release_appearing_after_inspection(host, monkeypatch):
    browser = bridge.installed_browsers()[0]
    original_write = bridge._write
    replacement = json.dumps(dict(name="io.codemyriad.cassini", path=str(executable(host / "release-native"))))

    def race(path, content, mode, **kwargs):
        if mode == "600" and path.name == "io.codemyriad.cassini.json":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(replacement)
        return original_write(path, content, mode, **kwargs)

    monkeypatch.setattr(bridge, "_write", race)
    with pytest.raises(OSError):
        bridge.register([browser])
    assert (browser.config / "NativeMessagingHosts/io.codemyriad.cassini.json").read_text() == replacement


def test_no_connection_poll_heals_dead_target_without_user_click(tmp_path, monkeypatch):
    browser = companion.Browser("Chromium", "/test/browser", tmp_path / "profile")
    live = executable(tmp_path / "live")
    monkeypatch.setattr(companion, "installed_browsers", lambda: [browser])
    monkeypatch.setattr(companion, "host_paths", lambda: hosts(live))
    monkeypatch.setattr(companion, "probe", lambda: (True, False))
    clock = Clock()
    controller = clock.connection(setup)
    controller.health_check = registration.health
    try:
        controller.trigger()
        clock.finish()
        path = browser.config / "NativeMessagingHosts/io.codemyriad.cassini.control.json"
        value = json.loads(path.read_text())
        value["path"] = str(tmp_path / "removed-build")
        path.write_text(json.dumps(value))
        controller.observe(dict(connectedBrowsers=0, browserNames=[]))
        clock.finish()  # health result schedules repair
        clock.finish()  # actual registration repair finishes
        assert json.loads(path.read_text())["path"] == str(live)
        assert controller.snapshot()["phase"] == "waiting"
    finally:
        controller.close()


def test_unchanged_registration_does_not_need_a_writable_directory(tmp_path):
    browser = companion.Browser("Chromium", "/test/browser", tmp_path / "profile")
    paths = companion.register(browsers=[browser], hosts=hosts(executable(tmp_path / "host")))
    directory = paths[0].parent
    (directory / ".cassini-registration.lock").unlink()
    before = {p: (p.read_bytes(), p.stat().st_ino) for p in paths}
    directory.chmod(0o500)
    try:
        companion.register(browsers=[browser], hosts=hosts(tmp_path / "host"))
        assert before == {p: (p.read_bytes(), p.stat().st_ino) for p in paths}
    finally:
        directory.chmod(0o700)
