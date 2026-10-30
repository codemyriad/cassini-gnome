#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D37 real no-browser, consent, no-transcript input and global-stop journeys."""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
if "--gtk-child" not in sys.argv:
    result = subprocess.run([sys.executable, __file__, "--gtk-child"], capture_output=True, text=True, timeout=100)
    print(result.stdout, end="")
    print(result.stderr, file=sys.stderr, end="")
    raise SystemExit(result.returncode or bool(re.search(r"Gtk-(WARNING|CRITICAL)", result.stderr)))
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gsk", "4.0")
from desktop_services import DesktopServices  # noqa: E402
from gi.repository import GdkX11, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import companion  # noqa: E402
from cassini_viewer import ux_formats as fmt  # noqa: E402
from cassini_viewer.browser_connection import BrowserConnection  # noqa: E402
from cassini_viewer.companion_setup import CompanionSetup  # noqa: E402
from cassini_viewer.global_shortcut import GlobalShortcut  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import update_metadata  # noqa: E402
from cassini_viewer.received import import_received  # noqa: E402


def until(check):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        context = GLib.MainContext.default()
        context.iteration(False)
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    raise AssertionError("D37 GUI condition timed out")


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def labels(widget):
    return [w.get_text() for w in widgets(widget) if isinstance(w, Gtk.Label)]


def focus(window):
    until(lambda: window.get_mapped() and window.get_width() > 0)
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True, timeout=3)
    until(window.is_active)


def key(window, symbol):
    focus(window)
    until(lambda: window.get_focus() is not None and window.get_focus().get_mapped())
    x11, xtst = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(None)
    assert display
    try:
        code = x11.XKeysymToKeycode(display, symbol)
        assert code
        xtst.XTestFakeKeyEvent(display, code, True, 0)
        xtst.XTestFakeKeyEvent(display, code, False, 0)
        x11.XSync(display, False)
    finally:
        x11.XCloseDisplay(display)


def click(widget, *, count=1):
    until(lambda: widget.get_mapped() and widget.get_width() > 0)
    native = widget.get_native()
    ok, bounds = widget.compute_bounds(native)
    assert ok
    xid = GdkX11.X11Surface.get_xid(native.get_surface())
    subprocess.run(
        [
            "xdotool",
            "mousemove",
            "--window",
            str(xid),
            str(round(bounds.get_x() + bounds.get_width() / 2)),
            str(round(bounds.get_y() + bounds.get_height() / 2)),
        ],
        check=True,
        timeout=3,
    )
    subprocess.run(["xdotool", "click", "--repeat", str(count), "--delay", "80", "1"], check=True, timeout=3)


errors = []


def callback_error(kind, value, tb):
    errors.append(str(value))
    sys.__excepthook__(kind, value, tb)


sys.excepthook = callback_error
services = DesktopServices()
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"))
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    app = CassiniApplication("com.gocassini.JourneyParityTest", "test", "development")
    app.transcription_enabled = False
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending)

    def no_browser():
        raise companion.SetupError("noBrowser")

    app.browser_connection = BrowserConnection(app._browser_connection_changed, attempt=no_browser)
    app.retry_browser_connection()
    until(lambda: app.browser_connection.future is None)
    assert app.browser_connection.snapshot()["phase"] == "needsBrowser"
    assert not window.browser_status.get_visible() and not window.first_browser_status.get_visible()
    assert not window.first_companion.get_visible()
    app.on_preferences()
    settings = app.settings_dialog
    assert settings.browser.get_title() == "Browser meetings need Chrome, Edge, Brave or Chromium."
    assert settings.browser_add.get_visible() and not settings.browser_add.get_sensitive()
    assert not settings.browser_setup.get_visible()
    settings.force_close()
    dialog = app.companion_setup = CompanionSetup(app)
    dialog.present(window)
    assert dialog.add_button.get_visible() and not dialog.add_button.get_sensitive()
    assert not dialog.connect_row.get_visible() and not dialog.add_retry.get_visible()
    assert dialog.add_caption.get_label() == "Browser meetings need Chrome, Edge, Brave or Chromium."
    dialog.force_close()
    controller = app.browser_connection
    controller.error = None
    controller.succeeded = True
    controller.browsers = (companion.Browser("Chromium", "/test/chromium", root / "not-opened"),)
    controller.observe(dict(connectedBrowsers=1, browserNames=["Chromium"]))
    assert app.recording_settings.get_boolean("browser-ever-connected")
    assert window.browser_status.get_visible()
    controller.observe(dict(connectedBrowsers=0))
    assert window.browser_status.get_visible() and window.browser_status.add.get_visible()
    controller.error = companion.SetupError("unwritable")
    app._browser_connection_changed()
    assert window.browser_status.retry.get_visible(), "A Library error must have its action"
    controller.error = companion.SetupError("noBrowser")
    controller.browsers = ()
    app._browser_connection_changed()
    assert not window.browser_status.get_visible(), "No-browser state must stay hidden even after a past connection"

    # Real Return and double-click on an audio-only Received meeting.
    received = import_received(ROOT / "ux/library-vectors/assets/plain-title.opus")
    update_metadata(received.directory, title="Audio only", titleSource="user")
    window.monitor.refresh_now()
    until(lambda: any(m.directory == received.directory for m in window.snapshot.meetings))
    meeting = next(m for m in window.snapshot.meetings if m.directory == received.directory)
    assert meeting.transcript == "none" and not meeting.words
    row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == meeting)
    window.rows.select_row(row)
    until(lambda: row.get_mapped())
    row.grab_focus()
    key(window, 0xFF0D)
    until(lambda: window.meeting_page is not None)
    page = window.meeting_page
    assert page.meeting.directory == received.directory and not page.meeting.words
    assert page.write_button.get_visible() and window.lookup_action("export").get_enabled()
    assert window.lookup_action("rename").get_enabled()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player._pipeline.set_property("audio-sink", sink)
    window.activate_action("win.play-pause", None)
    until(lambda: window.player.playing and window.player.position > 0)
    window.player.pause()
    written = []
    original_write = app.write_down
    app.write_down = lambda m: written.append(m.directory)
    page.write_button.emit("clicked")
    assert written == [received.directory]
    app.write_down = original_write
    window.show_export()
    assert window.export_dialog is not None and not window.export_dialog.closed
    window.export_dialog.cancel()
    window.show_rename()
    rename = window.rename_dialog
    until(lambda: rename.get_mapped())
    assert rename.entry.get_text() == "Audio only"
    rename.close()
    until(lambda: rename.closed)
    window.back_to_library()
    until(lambda: window.navigation.get_visible_page().get_child() == window.root_box)
    meeting = next(m for m in window.snapshot.meetings if m.directory == received.directory)
    row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == meeting)
    click(row)
    until(lambda: window.selected.directory == received.directory)
    assert window.meeting_page is None, "A single click should select the preview"
    click(row, count=2)
    until(lambda: window.meeting_page is not None)
    assert window.meeting_page.meeting.directory == received.directory
    window.back_to_library()

    queued = next(m for m in window.snapshot.meetings if m.transcript in ("queued", "running"))
    update_metadata(queued.directory, etaMinutes=3)
    window.monitor.refresh_now()
    until(lambda: any(m.directory == queued.directory and m.eta_minutes == 3 for m in window.snapshot.meetings))
    row = next(
        w for w in widgets(window.rows) if getattr(w, "meeting", None) and w.meeting.directory == queued.directory
    )
    window.rows.select_row(row)
    until(lambda: fmt.eta(3) in labels(window.preview))
    update_metadata(queued.directory, etaMinutes=2)
    window.monitor.refresh_now()
    until(lambda: fmt.eta(2) in labels(window.preview))

    # Only explicit Don't show again suppresses the repeated consent sheet.
    app.on_record()
    consent = app.record_first
    assert consent and consent.get_title() == "Before you record"
    assert consent.permission_captions["microphone"][0].get_label() != "Allowed"
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 3)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= 3)
    paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
    consent.close()
    until(lambda: app.record_first is None)
    app.on_record()
    assert app.record_first is not consent
    app.record_first.dont_show.set_active(True)
    app.record_first.close()
    until(lambda: app.record_first is None)
    requested = []
    original_start = app.start_recordings
    app.start_recordings = lambda: requested.append(True)
    app.on_record()
    assert requested == [True] and app.record_first is None
    app.start_recordings = original_start
    app.recording_settings.set_boolean("skip-record-first", False)
    app.start_native(test=True)
    until(lambda: app.recorder and app.recorder.pipeline and app._permission_sources == {"microphone", "system"})
    native = app.recorder
    app.stop_native()
    until(lambda: native.pipeline is None and not native._finishing)
    app.on_record()
    consent = app.record_first
    assert all(value[0].get_label() == "Allowed" for value in consent.permission_captions.values())
    consent.close()
    until(lambda: app.record_first is None)

    # A real portal activation while a different X11 window has focus.
    shortcut = app.global_shortcut = GlobalShortcut(app.on_global_record, connection=services.bus)
    until(lambda: shortcut.bound)
    app.start_native(test=True)
    until(
        lambda: (
            app.recorder is not native
            and app.recorder.pipeline
            and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        )
    )
    native = app.recorder
    other = Gtk.Window(title="Another app", child=Gtk.Label(label="Private focus target"))
    other.present()
    focus(other)
    until(lambda: not window.is_active())
    until(lambda: window.recording_page is not None)
    page = window.recording_page
    timeout_add = GLib.timeout_add
    with patch.object(
        GLib, "timeout_add", lambda interval, *a, **kw: timeout_add(10000 if interval == 250 else interval, *a, **kw)
    ):
        page.title_entry.set_text("Global stop title")
    assert page._dirty, "Fixture must retain the last debounced edit until global Stop"
    services.activate()
    identifier = "saved-" + native.session.directory.name
    until(lambda: native.pipeline is None and not native._finishing and identifier in services.notifications)
    assert services.notifications[identifier]["title"] == "Recording saved: Global stop title"
    assert not app._quitting
    other.destroy()
    window.present()
    focus(window)
    app.start_native(test=True)
    until(
        lambda: (
            app.recorder is not native
            and app.recorder.pipeline
            and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        )
    )
    native = app.recorder
    focus(window)
    services.activate()
    until(lambda: native.pipeline is None and not native._finishing)
    assert "saved-" + native.session.directory.name not in services.notifications
    shortcut.close()
    app.begin_exit()
    window.close()
    app.do_shutdown()
services.close()
assert not errors, errors
print(
    "PASS D37: neutral browser setup, latched Library status, Return/double-click/audio-only actions, ETA refresh, consent receipts and background shortcut saved notification"
)
