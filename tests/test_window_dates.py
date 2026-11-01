# SPDX-License-Identifier: GPL-3.0-or-later
"""Meeting details dates accept any GLib local zone identifier."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    gi = pytest.importorskip("gi")
    try:
        for namespace, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
            gi.require_version(namespace, version)
    except ValueError:
        pytest.skip("Meeting details dates also run in GTK-enabled distro and Recording jobs")
    from gi.repository import Gio

    compiler = shutil.which("glib-compile-resources")
    if compiler is None:
        pytest.skip("The application resource compiler is unavailable")
    source = Path(__file__).parents[1] / "src"
    resource = tmp_path_factory.mktemp("window-resources") / "cassini.gresource"
    subprocess.run(
        [
            compiler,
            str(source / "cassini-talk.gresource.xml"),
            "--sourcedir=" + str(source),
            "--target=" + str(resource),
        ],
        check=True,
    )
    Gio.resources_register(Gio.Resource.load(str(resource)))
    from cassini_talk import window

    return window


@pytest.mark.parametrize(
    "zone,expected",
    [
        ("+02:00", "Tuesday 27 October 2026 03:00"),
        (":/usr/share/zoneinfo/Europe/Rome", "Tuesday 27 October 2026 02:00"),
        ("Europe/Rome", "Tuesday 27 October 2026 02:00"),
    ],
)
def test_created_time_accepts_numeric_and_path_local_zones(window, monkeypatch, zone, expected):
    monkeypatch.setenv("TZ", zone)
    assert window.format_date("2026-10-27T01:00:00Z", time=True) == expected
