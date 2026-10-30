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


def test_profile_inventory_changes_only_for_browser_or_profile_changes(tmp_path, monkeypatch):
    browser = companion.Browser("Chromium", "/test/chromium", tmp_path / "chromium")
    monkeypatch.setattr(companion, "installed_browsers", lambda: [browser])
    monkeypatch.setattr(companion, "in_flatpak", lambda: False)
    missing = companion.profile_inventory()
    browser.config.mkdir()
    present = companion.profile_inventory()
    assert present != missing
    profile = browser.config / "Default"
    profile.mkdir()
    default = companion.profile_inventory()
    assert default != present
    (profile / "Preferences").write_text("irrelevant profile content")
    assert companion.profile_inventory() == default
    (browser.config / "Profile 2").mkdir()
    assert companion.profile_inventory() != default


def test_registers_both_hosts_atomically_with_exact_origin_and_private_permissions(tmp_path):
    browsers = companion.installed_browsers(config=tmp_path / "config", which=lambda name: "/bin/" + name)
    executable = tmp_path / "host"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    hosts = {"io.codemyriad.cassini": executable, "io.codemyriad.cassini.control": executable}
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
    hosts = {"io.codemyriad.cassini": executable}
    (destination,) = companion.register(browsers=[browser], hosts=hosts)
    old = destination.read_bytes()

    def failed(*_):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", failed)
    new = tmp_path / "updated-host"
    new.write_text("#!/bin/sh\n")
    new.chmod(0o700)
    with pytest.raises(OSError, match="rename"):
        companion.register(browsers=[browser], hosts={"io.codemyriad.cassini": new})
    assert destination.read_bytes() == old
    assert list(destination.parent.iterdir()) == [destination]


def test_an_unwritable_browser_does_not_prevent_other_browsers_registration(tmp_path, monkeypatch):
    browsers = [companion.Browser(name, "/test/browser", tmp_path / name) for name in ("bad", "good")]
    executable = tmp_path / "host"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    hosts = {"io.codemyriad.cassini": executable, "io.codemyriad.cassini.control": executable}
    original = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == browsers[0].config / "NativeMessagingHosts":
            raise PermissionError(13, "read-only browser config")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(PermissionError):
        companion.register(browsers=browsers, hosts=hosts)
    assert len(list(browsers[1].config.rglob("*.json"))) == 2


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
        "io.codemyriad.cassini": root / "native_host.py",
        "io.codemyriad.cassini.control": root / "control_host.py",
    }


def test_flatpak_without_the_host_bridge_never_offers_setup(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "com.gocassini.Viewer")
    assert not companion.available()


def test_snap_uses_its_real_profile_not_xdg_config(tmp_path):
    browsers = companion.installed_browsers(
        config=tmp_path / "custom",
        home=tmp_path,
        desktops={},
        host=False,
        which=lambda c: c if c == "/snap/bin/chromium" else None,
    )
    assert browsers == [companion.Browser("Chromium", "/snap/bin/chromium", tmp_path / "snap/chromium/common/chromium")]


def test_desktop_browser_with_absolute_executable_and_flatpak_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(companion, "in_flatpak", lambda: False)
    desktops = {
        "google-chrome.desktop": "[Desktop Entry]\nExec=/opt/google/chrome/google-chrome %U\n",
        "org.chromium.Chromium.desktop": "[Desktop Entry]\nExec=/usr/bin/flatpak run --branch=stable org.chromium.Chromium @@u %U @@\n",
        "com.brave.Browser.desktop": "[Desktop Entry]\nHidden=true\nExec=flatpak run com.brave.Browser\n",
    }
    browsers = companion.installed_browsers(
        config=tmp_path / "xdg",
        home=tmp_path,
        desktops=desktops,
        host=False,
        which=lambda c: {"/opt/google/chrome/google-chrome": c, "flatpak": "/usr/bin/flatpak"}.get(c),
    )
    assert len(browsers) == 2
    assert browsers[0].config == tmp_path / "xdg/google-chrome"
    chromium = browsers[1]
    assert chromium.config == tmp_path / ".var/app/org.chromium.Chromium/config/chromium"
    calls = []
    companion.open_add(chromium, launch=lambda command, **kw: calls.append(command))
    assert calls == [["/usr/bin/flatpak", "run", "org.chromium.Chromium", companion.ADD_URL]]


@pytest.mark.parametrize(
    ("name", "address"),
    [
        ("Chrome", "chrome://extensions/"),
        ("Chromium", "chrome://extensions/"),
        ("Edge", "edge://extensions/"),
        ("Brave", "brave://extensions/"),
    ],
)
def test_local_install_address_is_specific_to_the_browser(tmp_path, name, address):
    assert companion.extensions_address(companion.Browser(name, "/chosen/browser", tmp_path)) == address


def test_local_install_opens_a_new_blank_tab_without_launching_an_internal_url(tmp_path):
    calls = []
    browser = companion.Browser("Chromium", "/usr/bin/flatpak", tmp_path, ("run", "org.chromium.Chromium"))
    companion.open_new_tab(browser, launch=lambda *a, **kw: calls.append((a, kw)))
    ((args, options),) = calls
    assert args == (["/usr/bin/flatpak", "run", "org.chromium.Chromium", "--new-tab", "about:blank"],)
    assert "shell" not in options
