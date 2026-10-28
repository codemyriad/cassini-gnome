#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real setup dialog, per-user writes, pending/failure/retry and handshake gating."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

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
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
    assert texture.save_to_png(str(target.with_name(target.stem + suffix + ".png")))


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(tmp / "library"))
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    service = BrowserService(tmp / "browser.json")
    ready = threading.Event()
    path = tmp / "broker/browser.sock"
    thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(2)
    app = CassiniApplication("com.gocassini.CompanionTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    original = companion.installed_browsers
    browsers = original(config=tmp / "browser-config", which=lambda command: "/test/" + command)
    companion.installed_browsers = lambda: browsers
    launches = []
    companion.open_add = lambda browser: launches.append((browser.name, companion.ADD_URL))
    dialog = CompanionSetup(app, service_path=path)
    dialog.present(window)
    until(lambda: dialog.future is None)
    assert not dialog.done.get_sensitive()
    dialog.add_button.emit("clicked")
    until(lambda: len(launches) == 1)
    assert not dialog.done.get_sensitive() and dialog.add_caption.get_label() == "Not added yet"
    real_register = companion.register
    companion.register = lambda: (_ for _ in ()).throw(OSError("simulated read-only config"))
    dialog.connect_button.emit("clicked")
    until(lambda: dialog.failed)
    assert dialog.connect_button.get_label() == "Try again" and not dialog.done.get_sensitive()
    shot(window, "-failed")
    companion.register = real_register
    dialog.connect_button.emit("clicked")
    until(lambda: dialog.deadline is not None)
    assert len(list((tmp / "browser-config").rglob("com.codemyriad.cassini*.json"))) == 8
    assert not dialog.done.get_sensitive() and dialog.connect_caption.get_label() == "Connecting…"
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
        assert dialog.connect_caption.get_label() == "Connected."
        shot(window, "-connected")
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        shot(window, "-connected-dark")
    until(lambda: not dialog.done.get_sensitive())
    assert dialog.connect_button.get_visible()
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
