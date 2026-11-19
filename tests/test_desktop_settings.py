# SPDX-License-Identifier: GPL-3.0-or-later
"""D107 §4: Open Settings reaches the desktop's Settings, on the host in Flatpak."""

import subprocess

import pytest

from cassini_talk import desktop_settings, flatpak_bridge


class Process:
    def __init__(self, result=None):
        self.result = result

    def wait(self, timeout):
        if self.result is None:
            raise subprocess.TimeoutExpired("settings", timeout)
        return self.result


def launcher(calls, result=None):
    def launch(argv, **options):
        calls.append((argv, options))
        return Process(result)

    return launch


def test_native_opens_the_panel_directly(monkeypatch):
    monkeypatch.setattr(desktop_settings, "in_flatpak", lambda: False)
    calls = []
    desktop_settings.open_panel("privacy", launch=launcher(calls))
    assert calls[0][0] == ["gnome-control-center", "privacy"]
    assert calls[0][1]["start_new_session"] and calls[0][1]["stdin"] == subprocess.DEVNULL


def test_flatpak_dispatches_to_the_host(monkeypatch):
    monkeypatch.setattr(desktop_settings, "in_flatpak", lambda: True)
    monkeypatch.setattr(flatpak_bridge.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []
    desktop_settings.open_panel("privacy", launch=launcher(calls, result=0))
    argv = calls[0][0]
    assert argv[:3] == ["/usr/bin/flatpak-spawn", "--host", "--directory=/"]
    assert "--watch-bus" not in argv  # Settings outlives the sandbox's bus.
    assert argv[3:5] == ["/bin/sh", "-c"] and argv[5] == flatpak_bridge.BROWSER_LAUNCH
    assert argv[-2:] == ["gnome-control-center", "privacy"]


def test_failures_are_raised(monkeypatch):
    monkeypatch.setattr(desktop_settings, "in_flatpak", lambda: True)
    monkeypatch.setattr(flatpak_bridge.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(OSError):
        desktop_settings.open_panel("privacy", launch=launcher([], result=127))
    monkeypatch.setattr(flatpak_bridge.shutil, "which", lambda name: None)
    with pytest.raises(OSError):
        desktop_settings.open_panel("privacy", launch=launcher([]))
