# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed-origin, user-only registration and accurate installation detection."""

import base64
import hashlib
import json
import stat
from pathlib import Path

import pytest

from cassini_viewer import companion


def test_registration_origin_matches_the_pinned_manifest_key():
    manifest = json.loads((Path(__file__).parents[1] / "browser-extension/manifest.json").read_text())
    raw = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    identifier = "".join(chr(ord("a") + int(char, 16)) for char in raw)
    assert identifier == companion.EXTENSION_ID
    assert f"chrome-extension://{identifier}/" == companion.ORIGIN


def test_detects_the_four_installed_browsers_under_this_users_config(tmp_path):
    commands = {"google-chrome-stable", "chromium-browser", "microsoft-edge", "brave"}
    browsers = companion.installed_browsers(
        config=tmp_path, which=lambda name: "/test/" + name if name in commands else None
    )
    assert [b.name for b in browsers] == ["Chrome", "Chromium", "Edge", "Brave"]
    assert [b.config.relative_to(tmp_path).as_posix() for b in browsers] == [
        "google-chrome",
        "chromium",
        "microsoft-edge",
        "BraveSoftware/Brave-Browser",
    ]


def test_no_manifests_are_written_for_missing_browsers(tmp_path):
    assert companion.installed_browsers(config=tmp_path, which=lambda _: None) == []
    with pytest.raises(OSError, match="No supported browser"):
        companion.register(browsers=[], hosts={})
    assert list(tmp_path.iterdir()) == []


def test_registers_both_hosts_atomically_with_exact_origin_and_private_permissions(tmp_path):
    browsers = companion.installed_browsers(config=tmp_path / "config", which=lambda name: "/bin/" + name)
    executable = tmp_path / "host"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    hosts = {"com.codemyriad.cassini": executable, "com.codemyriad.cassini.control": executable}
    written = companion.register(browsers=browsers, hosts=hosts)
    assert len(written) == 8
    for path in written:
        data = json.loads(path.read_text())
        assert data["allowed_origins"] == [companion.ORIGIN]
        assert data["path"] == str(executable) and data["type"] == "stdio"
        assert data["name"] == path.stem
        assert path.is_relative_to(tmp_path / "config")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert companion.register(browsers=browsers, hosts=hosts) == written
    assert all(sorted(p.name for p in path.parent.iterdir()) == sorted(n + ".json" for n in hosts) for path in written)


def test_failed_manifest_publication_preserves_old_registration_and_removes_temporary(tmp_path, monkeypatch):
    executable = tmp_path / "host"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    browser = companion.Browser("Chromium", "/bin/chromium", tmp_path / "config")
    hosts = {"com.codemyriad.cassini": executable}
    (destination,) = companion.register(browsers=[browser], hosts=hosts)
    old = destination.read_bytes()

    def failed(*_):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", failed)
    with pytest.raises(OSError, match="rename"):
        companion.register(browsers=[browser], hosts=hosts)
    assert destination.read_bytes() == old
    assert list(destination.parent.iterdir()) == [destination]


def test_add_opens_the_approved_store_url_in_the_chosen_browser_without_a_shell(tmp_path):
    calls = []
    browser = companion.Browser("Brave", "/chosen/brave", tmp_path)
    companion.open_add(browser, launch=lambda *args, **kwargs: calls.append((args, kwargs)))
    ((args, kwargs),) = calls
    assert args == (["/chosen/brave", companion.ADD_URL],)
    assert "shell" not in kwargs


def test_source_checkout_registers_its_own_hosts_before_any_installed_old_version(monkeypatch):
    monkeypatch.setattr(companion.shutil, "which", lambda _: "/old/unrelated/native-host")
    hosts = companion.host_paths()
    root = Path(__file__).parents[1] / "browser-extension"
    assert hosts == {
        "com.codemyriad.cassini": root / "native_host.py",
        "com.codemyriad.cassini.control": root / "control_host.py",
    }
