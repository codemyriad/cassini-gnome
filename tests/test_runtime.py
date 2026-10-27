# SPDX-License-Identifier: GPL-3.0-or-later

import sys
import types

from cassini_viewer import runtime


def test_too_old():
    assert runtime.too_old("GTK", (4, 18, 0), (4, 18)) is None
    assert runtime.too_old("GTK", (4, 20, 1), (4, 18)) is None
    assert runtime.too_old("GTK", (5, 0, 0), (4, 18)) is None
    assert runtime.too_old("GTK", (4, 16, 7), (4, 18)) == "GTK 4.18 or newer is needed, but 4.16.7 is installed"
    assert runtime.too_old("libadwaita", (1, 7, 2), (1, 8)) is not None


def test_message_names_every_problem():
    text = runtime.message(["first problem", "second problem"])
    assert text.startswith("Cassini cannot start: first problem; second problem.")
    assert "Install the missing packages" in text
    assert "\n" not in text


def test_message_for_old_versions_suggests_upgrading():
    text = runtime.message([runtime.too_old("libadwaita", (1, 7, 6), (1, 8))])
    assert "libadwaita 1.8 or newer is needed, but 1.7.6 is installed" in text
    assert "Upgrade your distribution" in text
    assert "Flatpak" in text
    # A missing package alongside an old version still needs installing.
    assert "Install the missing packages" in runtime.message([runtime.too_old("GTK", (4, 16), (4, 18)), "x"])


def test_missing_gst_elements():
    available = {"oggdemux": object()}
    assert runtime.missing_gst_elements(available.get) == ["opusdec"]
    assert runtime.missing_gst_elements(lambda _name: object()) == []
    assert "opusdec" in runtime.gst_warning(["opusdec"])
    assert runtime.missing_gst_elements(available.get, runtime.playback_elements("file:///a/track-1.wav")) == [
        "wavparse"
    ]
    assert runtime.playback_elements("file:///a/Meeting.OPUS") == ("oggdemux", "opusdec")


def test_recording_warning_names_the_pulseaudio_plugin():
    missing = runtime.missing_gst_elements({"opusenc": 1, "oggmux": 1}.get, runtime.RECORDING_ELEMENTS)
    assert missing == ["pulsesrc", "audiointerleave"]
    text = runtime.recording_warning(missing)
    assert "pulsesrc, audiointerleave" in text
    assert "gstreamer1.0-pulseaudio" in text
    assert "gstreamer1.0-pulseaudio" not in runtime.plugins_for(["opusenc"])


def test_old_python(monkeypatch):
    monkeypatch.setattr(runtime.sys, "version_info", (3, 10, 12, "final", 0))
    problems = runtime._problems()
    assert problems == ["Python 3.11 or newer is needed, but 3.10 is installed"]
    assert "Upgrade your distribution" in runtime.message(problems)


def test_gi_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    problems = runtime._problems()
    assert len(problems) == 1
    assert problems[0].startswith("the Python module 'gi' is missing; install PyGObject (python3-gi")
    assert "Install the missing packages" in runtime.message(problems)


def test_typelibs_missing(monkeypatch):
    def require_version(namespace, _api):
        if namespace != "Gtk":
            raise ValueError(f"Namespace {namespace} not available")

    monkeypatch.setitem(sys.modules, "gi", types.SimpleNamespace(require_version=require_version))
    problems = runtime._problems()
    assert len(problems) == 2
    assert problems[0].startswith("the Adw 1 introspection data is missing; install libadwaita (gir1.2-adw-1")
    assert problems[1].startswith("the Gst 1.0 introspection data is missing; install GStreamer (")


def test_dependent_typelib_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", types.SimpleNamespace(require_version=lambda *_args: None))
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    problems = runtime._problems()
    assert len(problems) == 1
    assert problems[0].startswith("loading GTK, libadwaita and GStreamer failed (")
    assert "gobject-introspection" in problems[0]


def test_check_passes_or_exits_with_message(capsys):
    # Runs against whatever the test machine has; either outcome must be clean.
    try:
        runtime.check()
    except SystemExit as exc:
        assert exc.code == 1
        assert capsys.readouterr().err.startswith("Cassini cannot start: ")
