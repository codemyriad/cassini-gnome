# SPDX-License-Identifier: GPL-3.0-or-later

import os
import socket
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

from cassini_talk import runtime


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


def test_all_elements_covers_playback_and_recording_once():
    names = runtime.all_elements()
    assert names[0] == "playbin"
    assert len(names) == len(set(names))
    for wanted in runtime.PLAYBACK_ELEMENTS.values():
        assert set(wanted) <= set(names)
    assert set(runtime.RECORDING_ELEMENTS) <= set(names)


def test_sanitize_accessibility_env_drops_stale_unix_sockets_and_preserves_live_listener(tmp_path):
    assert runtime.sanitize_accessibility_env({}) is False
    assert runtime.sanitize_accessibility_env({"AT_SPI_BUS_ADDRESS": ""}) is False

    missing_env = {"AT_SPI_BUS_ADDRESS": f"unix:path={tmp_path / 'missing-bus'},guid=0123456789abcdef"}
    assert runtime.sanitize_accessibility_env(missing_env) is True
    assert "AT_SPI_BUS_ADDRESS" not in missing_env

    regular = tmp_path / "plain-file"
    regular.write_text("not a socket")
    regular_env = {"AT_SPI_BUS_ADDRESS": f"unix:path={regular}"}
    assert runtime.sanitize_accessibility_env(regular_env) is True
    assert "AT_SPI_BUS_ADDRESS" not in regular_env

    if not hasattr(socket, "AF_UNIX"):
        return

    spaced = Path("/tmp") / f"cassini atspi {tmp_path.name}"
    spaced.mkdir(parents=True, exist_ok=True)
    sock_path = spaced / "bus_0"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        server.listen(1)
        server.setblocking(False)
        encoded = quote(str(sock_path), safe="/")
        live_env = {"AT_SPI_BUS_ADDRESS": f"unix:path={encoded},guid=0123456789abcdef"}
        assert runtime.sanitize_accessibility_env(live_env) is False
        assert live_env["AT_SPI_BUS_ADDRESS"].startswith("unix:path=")
        with pytest.raises(BlockingIOError):
            server.accept()
    finally:
        server.close()

    # Closed listener leaving a stale socket inode on disk must also be stripped.
    try:
        assert sock_path.exists()
        closed_env = {"AT_SPI_BUS_ADDRESS": f"unix:path={sock_path},guid=0123456789abcdef"}
        assert runtime.sanitize_accessibility_env(closed_env) is True
        assert "AT_SPI_BUS_ADDRESS" not in closed_env
    finally:
        sock_path.unlink(missing_ok=True)
        spaced.rmdir()


def test_runtime_check_strips_stale_at_spi_bus_address_without_gtk_critical(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(
        os.environ,
        AT_SPI_BUS_ADDRESS=f"unix:path={tmp_path / 'stale-at-spi-bus'},guid=0123456789abcdef",
    )
    script = (
        "import os\n"
        "from cassini_talk import runtime\n"
        "try:\n"
        "    runtime.check()\n"
        "except SystemExit:\n"
        "    pass\n"
        "assert 'AT_SPI_BUS_ADDRESS' not in os.environ\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=source, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Gtk-CRITICAL" not in result.stderr
    assert "Unable to connect to the accessibility bus" not in result.stderr
