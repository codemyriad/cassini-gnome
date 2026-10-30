#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real setup dialog, per-user writes, pending/failure/retry and handshake gating."""

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
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import companion  # noqa: E402
from cassini_viewer.browser_service import BrowserService, connect, request, send_packet  # noqa: E402
from cassini_viewer.companion_setup import CompanionSetup  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402


def until(check):
    end = time.monotonic() + 10
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Companion GUI condition timed out"


def shot(window, suffix):
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"])
    paintable = Gtk.WidgetPaintable.new(window)
    frames = []
    window.add_tick_callback(lambda *_: (frames.append(1), len(frames) < 3)[1])
    window.queue_draw()
    until(lambda: len(frames) >= 2)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    assert texture.save_to_png(str(target.with_name(target.stem + suffix + ".png")))


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    fixtures = tmp / "fixtures"
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(fixtures)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(fixtures / "main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(fixtures / "main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Gtk.Settings.get_default().props.gtk_font_name = "Cantarell 11"
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    service = BrowserService(tmp / "browser.json")
    ready = threading.Event()
    path = tmp / "broker/browser.sock"
    thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(2)
    legacy = Path(os.environ["XDG_CONFIG_HOME"]) / "chromium/NativeMessagingHosts/com.codemyriad.cassini.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps(dict(name="com.codemyriad.cassini", path="/old/cassini-browser-host")))
    original = companion.installed_browsers
    browsers = original(config=tmp / "browser-config", which=lambda command: "/test/" + command)
    companion.installed_browsers = lambda: browsers
    launches = []
    companion.open_add = lambda browser: launches.append((browser.name, companion.ADD_URL))
    app = CassiniApplication("com.gocassini.CompanionTest", "test", "development")
    # Actual launch starts registration, with only private configs/executables.
    # Browser transport is supplied by the real private service below.
    app._start_browser_watch = lambda: None
    os.environ.pop("CASSINI_SCREENSHOT_ISOLATED")
    with patch.dict(os.environ, {"CASSINI_ISOLATED_AUDIO": "1"}):
        app.register(None)
        until(lambda: not legacy.exists())
    os.environ["CASSINI_SCREENSHOT_ISOLATED"] = "1"
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending and app.browser_connection.future is None)
    assert app.browser_connection.snapshot()["phase"] == "waiting"
    files = list((tmp / "browser-config").rglob("io.codemyriad.cassini*.json"))
    assert len(files) == 8, "Both hosts must be registered at launch without Connect"
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in files}
    from cassini_viewer.settings_dialog import SettingsDialog

    settings = app.settings_dialog = SettingsDialog(app, service_path=path)
    assert settings.browser.get_title() == "Waiting for the Cassini Recorder extension"
    assert settings.browser_add.get_visible() and not settings.browser_setup.get_visible()
    dialog = app.companion_setup = CompanionSetup(app, service_path=path)
    dialog.present(window)
    until(lambda: dialog.future is None)
    assert not dialog.done.get_sensitive() and not dialog.connect_button.get_visible()
    shot(window, "-pending")
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    shot(window, "-pending-dark")
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    dialog.add_button.emit("clicked")
    until(lambda: len(launches) == 1)
    assert not dialog.done.get_sensitive() and dialog.add_caption.get_label() == "Not added yet"
    opener = companion.open_add

    def failed_open(_browser):
        raise OSError("synthetic browser launch failure")

    companion.open_add = failed_open
    dialog.add_button.emit("clicked")
    until(lambda: dialog.local_error is not None)
    assert dialog.failed and dialog.connect_button.get_visible()
    companion.open_add = opener
    dialog.add_button.emit("clicked")
    until(lambda: len(launches) == 2 and dialog.local_error is None and not dialog.failed)
    real_register = companion.register

    def read_only(**_kwargs):
        raise PermissionError(13, "simulated read-only config")

    companion.register = read_only
    app.retry_browser_connection()  # A profile-change trigger, not a Connect click.
    until(lambda: dialog.failed)
    assert dialog.connect_button.get_label() == "Try again" and dialog.connect_button.get_visible()
    assert settings.browser_setup.get_visible() and not settings.browser_add.get_visible()
    assert not app.browser_connection.timer, "Permanent config errors must stop retrying"
    shot(window, "-failed")
    companion.register = real_register
    dialog.connect_button.emit("clicked")
    until(lambda: app.browser_connection.future is None and not dialog.failed)
    assert not dialog.connect_button.get_visible() and not dialog.done.get_sensitive()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in files}
    assert dialog.connect_caption.get_label() == "Waiting for the Cassini Recorder extension"
    # The native host's socket connection alone is not an extension handshake.
    with connect(path) as connection, connection.makefile("rwb") as stream:
        send_packet(stream, dict(role="extension", browserName="Chrome"))
        until(lambda: request(path=path)["browsers"] == 1)
        dialog.poll()
        until(lambda: dialog.future is None)
        assert not dialog.done.get_sensitive()
        send_packet(stream, dict(type="hello", version=1))
        send_packet(stream, dict(type="status", recording=False))
        until(lambda: dialog.done.get_sensitive())
        assert dialog.add_caption.get_label() == "Done. Cassini is in Chrome."
        assert dialog.connect_caption.get_label() == "Connected to Chrome"
        assert settings.browser.get_title() == "Connected to Chrome"
        assert not settings.browser_setup.get_visible() and not settings.browser_add.get_visible()
        shot(window, "-connected")
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        shot(window, "-connected-dark")
    until(lambda: not dialog.done.get_sensitive())
    assert not dialog.connect_button.get_visible()
    settings.force_close()
    dialog.force_close()
    until(lambda: dialog.closed)
    window.close()
    app.do_shutdown()
    service.closed.set()
    thread.join(2)
    assert not thread.is_alive()
    print(
        "PASS Companion setup: Add never fabricates installation; eight per-user manifests; failed/retry; Done requires hello/status; disconnect clears Done"
    )
