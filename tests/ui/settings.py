#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Preferences switches, persisted portal binding and browser broker controls."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.browser_service import BrowserService, request, send_packet  # noqa: E402
from cassini_viewer.browser_service import connect as broker_connect  # noqa: E402
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.player import Player  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402


def until(check):
    end = time.monotonic() + 12
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Settings GUI condition timed out"


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(tmp / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(tmp / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    connect = LibraryWindow._connect_player

    def private_connect(window):
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", True)
        if isinstance(window.player, FolderPlayer):
            window.player.audio_sink = sink
        elif isinstance(window.player, Player):
            window.player._pipeline.set_property("audio-sink", sink)
        connect(window)

    LibraryWindow._connect_player = private_connect
    service = BrowserService(tmp / "browser-config.json")
    ready = threading.Event()
    path = tmp / "broker/browser.sock"
    thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(2)
    desktop = DesktopServices()
    app = CassiniApplication("com.gocassini.SettingsTest", "test", "development")
    # Exercise real startup without the screenshot/audio opt-out. Everything,
    # including the desktop portal service and browser profiles, is private.
    with patch.dict(os.environ):
        os.environ.pop("CASSINI_SCREENSHOT_ISOLATED", None)
        os.environ.pop("CASSINI_ISOLATED_AUDIO", None)
        app.register(None)
    assert app.global_shortcut is None, "Fresh startup must not ask for global shortcuts"
    assert not desktop.calls
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    from cassini_viewer.settings_dialog import SettingsDialog

    dialog = SettingsDialog(app, service_path=path)
    dialog.present(window)
    until(lambda: dialog._future is None)
    assert len(dialog.pages) == 4
    assert dialog.get_title() == "Preferences" and not dialog.get_search_enabled()
    assert dialog.explain.get_active()
    dialog.explain.set_active(False)
    assert app.recording_settings.get_boolean("skip-record-first")
    dialog.explain.set_active(True)
    assert not app.recording_settings.get_boolean("skip-record-first")
    assert dialog.shortcut.get_subtitle() == "Ctrl+Alt+R"
    assert not app.recording_settings.get_boolean("record-shortcut-enabled")
    assert dialog.change_shortcut.get_sensitive() and not desktop.calls
    # Declining must leave opt-in false and allow another explicit attempt.
    desktop.denied = True
    dialog.change_shortcut.emit("clicked")
    until(lambda: app.global_shortcut and app.global_shortcut.error)
    assert not app.recording_settings.get_boolean("record-shortcut-enabled")
    dialog.refresh()
    assert dialog.change_shortcut.get_sensitive()
    desktop.denied = False
    dialog.change_shortcut.emit("clicked")
    pending = app.global_shortcut
    dialog.change_shortcut.emit("clicked")
    assert app.global_shortcut is pending, "Repeated clicks must not create overlapping sessions"
    until(lambda: app.global_shortcut.bound)
    assert app.recording_settings.get_boolean("record-shortcut-enabled")
    assert [c["method"] for c in desktop.calls] == ["CreateSession", "CreateSession", "ListShortcuts", "BindShortcuts"]
    desktop.shortcut_description = "Ctrl+Shift+R"
    dialog.change_shortcut.emit("clicked")
    until(lambda: app.global_shortcut.trigger_description == "Ctrl+Shift+R")
    dialog.refresh()
    assert dialog.shortcut.get_subtitle() == "Ctrl+Shift+R"
    assert app.recording_settings.get_string("record-shortcut-description") == "Ctrl+Shift+R"
    assert any(c["method"] == "ConfigureShortcuts" for c in desktop.calls)
    stale = request(path=path)
    dialog.automatic.set_active(True)
    app._browser_changed(stale)
    assert app._auto_recording_held, "An old poll undid an accepted auto-record choice"
    until(lambda: service.auto_record and dialog._future is None and app._auto_recording_pending is None)
    app._browser_changed(stale)
    assert app._auto_recording_held, "An old poll undid the completed preference write"
    assert json.loads((tmp / "browser-config.json").read_text())["autoRecord"] is True
    with broker_connect(path) as browser:
        stream = browser.makefile("rwb")
        send_packet(stream, dict(role="extension", browserName="Chromium"))
        send_packet(stream, dict(type="hello", version=1))
        send_packet(stream, dict(type="status", recording=False))
        until(lambda: request(path=path)["connectedBrowsers"] == 1)
        dialog.refresh()
        until(lambda: dialog.browser.get_title() == "Connected to Chromium")
        assert "Parakeet Ultra" not in labels(dialog) and "Tell speakers apart" not in labels(dialog)
        assert "In use for new meetings" in labels(dialog)
        assert "To check for updates. No meeting data is sent." not in labels(dialog)
        stream.close()
    with broker_connect(path) as unknown:
        stream = unknown.makefile("rwb")
        send_packet(stream, dict(role="extension"))
        send_packet(stream, dict(type="hello", version=1))
        send_packet(stream, dict(type="status", recording=False))
        until(lambda: request(path=path)["connectedBrowsers"] == 1)
        dialog.refresh()
        until(lambda: dialog.browser.get_title() == "Connected")
        stream.close()
    dialog.force_close()
    until(lambda: dialog.closed)
    # The second dialog is closed before its initial status delivery. Its
    # explicit choice must still be written after the observation is cancelled.
    second = SettingsDialog(app, service_path=path)
    second.present(window)
    until(lambda: second._future is None)
    assert second.automatic.get_active()
    second.refresh()
    second.automatic.set_active(False)
    second.force_close()
    until(lambda: second.closed and not service.auto_record and app._auto_recording_pending is None)
    assert not app._auto_recording_held
    assert json.loads((tmp / "browser-config.json").read_text())["autoRecord"] is False
    app.global_shortcut.close()
    restored = CassiniApplication("com.gocassini.SettingsRestoreTest", "test", "development")
    with patch.dict(os.environ):
        os.environ.pop("CASSINI_SCREENSHOT_ISOLATED", None)
        os.environ.pop("CASSINI_ISOLATED_AUDIO", None)
        restored.register(None)
    until(lambda: restored.global_shortcut and restored.global_shortcut.bound)
    assert restored.global_shortcut.trigger_description == "Ctrl+Shift+R"
    binding = [c for c in desktop.calls if c["method"] == "BindShortcuts"][-1]
    assert "preferred_trigger" not in binding["values"][1][0][1]
    restored.do_shutdown()
    desktop.close()
    window.close()
    app.do_shutdown()
    service.closed.set()
    thread.join(2)
    assert not thread.is_alive()
    print(
        "PASS Preferences: four pages, first-record binding, portal configure/cache, broker auto-record/status and immediate close"
    )
