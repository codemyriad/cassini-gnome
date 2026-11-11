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


@pytest.mark.parametrize(
    "recorded,local,expected",
    [
        ("2026-11-09T23:31:48Z", None, "Tuesday 10 November 2026"),
        (None, "2026-11-09T23:31:48", "Monday 9 November 2026"),
        (None, None, None),
    ],
)
def test_standalone_subtitle_uses_recording_time_and_omits_seal_time(
    window, tmp_path, monkeypatch, recorded, local, expected
):
    from types import SimpleNamespace

    from test_recorded_at import changed_file

    from cassini_talk import portable

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    path = changed_file(tmp_path, recorded=recorded, local=local, created="2026-11-20T09:40:48Z")
    labels = []
    document = portable.read_meeting(str(path))
    controller = SimpleNamespace(
        meeting=document,
        subtitle_label=SimpleNamespace(set_label=labels.append),
        _speaker_ids=lambda: [speaker.id for speaker in document.speakers],
    )
    window.CassiniWindow._update_subtitle(controller)
    assert len(labels) == 1
    assert "20 November" not in labels[0]
    if expected is None:
        assert "2026" not in labels[0]
    else:
        assert expected in labels[0]
