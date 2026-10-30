#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D35 actual row/Meeting popovers, F2/Return/Escape and atomic failure retry."""

import ctypes
import hashlib
import os
import re
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
if "--gtk-child" not in sys.argv:
    result = subprocess.run([sys.executable, __file__, "--gtk-child"], capture_output=True, text=True, timeout=90)
    print(result.stdout, end="")
    print(result.stderr, file=sys.stderr, end="")
    raise SystemExit(result.returncode or bool(re.search(r"Gtk-(WARNING|CRITICAL)", result.stderr)))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import GdkX11, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import read_metadata  # noqa: E402


def until(check):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        context = GLib.MainContext.default()
        context.iteration(False)  # Poll native X11 input, not only already-ready idle/timer sources.
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    if not check():
        dialog = globals().get("dialog")
        print(
            "Native key events:",
            globals().get("key_events"),
            "popover mapped:",
            globals().get("popover").get_mapped() if globals().get("popover") else None,
            flush=True,
        )
        if dialog is not None:
            print(
                "Rename timeout:",
                dict(
                    closed=dialog.closed,
                    busy=dialog.busy,
                    failed=dialog.failed,
                    entered=dialog.entry.get_text(),
                    confirm=dialog.confirm.get_sensitive(),
                    futureDone=dialog.future.done() if dialog.future else None,
                    futureError=str(dialog.future.exception()) if dialog.future and dialog.future.done() else None,
                ),
                flush=True,
            )
        raise AssertionError("Rename UI condition timed out")


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def labels(widget):
    return [w.get_text() for w in widgets(widget) if isinstance(w, Gtk.Label)]


def choose_rename(popover):
    until(popover.get_mapped)
    buttons = [w for w in widgets(popover) if w.__gtype__.name == "GtkModelButton"]
    assert "Rename…" in labels(buttons[0]), "Rename must be first"
    item = buttons[0]
    until(lambda: item.get_mapped() and item.get_width() > 0)
    assert item.is_sensitive()
    assert item.activate()


def key(window, symbol):
    until(
        lambda: (
            window.get_focus() is not None and window.get_focus().get_mapped() and window.get_focus().get_width() > 0
        )
    )
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    window.get_display().sync()
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True)
    until(window.is_active)
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


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    app = CassiniApplication("com.gocassini.RenameTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    key_events = []
    controller = Gtk.EventControllerKey()
    controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    controller.connect(
        "key-pressed", lambda _controller, value, code, state: key_events.append((value, code, int(state))) or False
    )
    window.add_controller(controller)
    until(lambda: not window._pending)
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True)
    until(window.is_active)
    chosen = next(m for m in window.snapshot.meetings if m.title == "Budget review")
    folder = chosen.directory
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.name != "meeting.json"}
    for surface, name in (("row", "Row renamed"), ("meeting", "Meeting renamed")):
        chosen = next(m for m in window.snapshot.meetings if m.directory == folder)
        if surface == "row":
            row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == chosen)
            popover = window._row_menu(row, 100, 20)
        else:
            window.open_meeting(chosen)
            button = next(
                w
                for w in widgets(window.meeting_page)
                if isinstance(w, Gtk.MenuButton) and w.get_menu_model() == window.meeting_page.more_menu
            )
            button.popup()
            popover = button.get_popover()
        choose_rename(popover)
        until(lambda: window.rename_dialog is not None and not window.rename_dialog.closed)
        dialog = window.rename_dialog
        assert dialog.entry.get_text() == chosen.title
        until(lambda dialog=dialog, chosen=chosen: dialog.entry.get_selection_bounds() == (0, len(chosen.title)))
        assert not dialog.confirm.get_sensitive()
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect(
            "key-pressed", lambda _controller, value, code, state: key_events.append((value, code, int(state))) or False
        )
        dialog.entry.get_delegate().add_controller(controller)
        if surface == "meeting":
            ticks = []
            window.add_tick_callback(lambda *_, ticks=ticks: (ticks.append(True), len(ticks) < 3)[1])
            window.queue_draw()
            until(lambda ticks=ticks: len(ticks) >= 3)
            paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
            paintable.snapshot(snapshot, window.get_width(), window.get_height())
            node = snapshot.to_node()
            texture = window.get_renderer().render_texture(node, node.get_bounds())
            assert (texture.get_width(), texture.get_height()) == (1120, 720)
            assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
        until(lambda dialog=dialog: dialog.entry.get_mapped() and dialog.entry.get_width() > 0)
        dialog.entry.set_text("  " + name + "  ")
        until(lambda dialog=dialog: window.get_focus() == dialog.entry.get_delegate())
        # Keep the save pending beyond GtkText's cursor blink callback. Disabling
        # the focused inner text without a focus-out produces a delayed warning.
        from cassini_viewer.meeting_rename import rename_meeting

        release = threading.Event()

        def paused_save(*args, release=release):
            assert release.wait(8), "GUI did not release the metadata worker"
            return rename_meeting(*args)

        with patch("cassini_viewer.rename_dialog.rename_meeting", side_effect=paused_save):
            key(window, 0xFF0D)  # Return through the actual entry/default button.
            until(lambda dialog=dialog: dialog.busy)
            started = time.monotonic()
            try:
                until(lambda started=started: time.monotonic() - started >= 1.5)
                assert not dialog.closed and dialog.busy
            finally:
                release.set()
            until(
                lambda dialog=dialog, name=name: (
                    dialog.closed and any(m.title == name for m in window.snapshot.meetings)
                )
            )
        assert read_metadata(folder)["titleSource"] == "user"
        if surface == "meeting":
            assert window.meeting_page.headline.get_title() == name and window.get_title() == name
            window.back_to_library()
            assert window.get_title() == "Library"
    chosen = next(m for m in window.snapshot.meetings if m.directory == folder)
    row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == chosen)
    window.rows.select_row(row)
    until(lambda: row.get_mapped() and row.get_width() > 0)
    row.grab_focus()
    until(lambda: window.get_focus() is row)
    key(window, 0xFFBF)  # F2.
    until(lambda: window.rename_dialog and not window.rename_dialog.closed)
    dialog = window.rename_dialog
    dialog.entry.set_text("Cancelled name")
    previous = (folder / "meeting.json").read_bytes()
    key(window, 0xFF1B)
    until(lambda: dialog.closed)
    assert (folder / "meeting.json").read_bytes() == previous
    until(lambda: row.get_mapped() and row.get_width() > 0)
    row.grab_focus()
    until(lambda: window.get_focus() is row)
    key(window, 0xFFBF)
    until(lambda: window.rename_dialog is not dialog and not window.rename_dialog.closed)
    dialog = window.rename_dialog
    dialog.entry.set_text("Retry name")
    with patch("cassini_viewer.rename_dialog.rename_meeting", side_effect=PermissionError("read-only fixture")):
        dialog.confirm.emit("clicked")
        until(lambda: not dialog.busy)
        assert not dialog.closed and dialog.problem.get_visible() and dialog.entry.get_text() == "Retry name"
        assert (folder / "meeting.json").read_bytes() == previous
        assert dialog.confirm.get_label() == "Try again"
    dialog.confirm.emit("clicked")
    until(lambda: dialog.closed and read_metadata(folder)["title"] == "Retry name")
    until(lambda: any(m.title == "Retry name" for m in window.snapshot.meetings))
    chosen = next(m for m in window.snapshot.meetings if m.directory == folder)
    row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == chosen)
    window.rows.select_row(row)
    until(lambda: row.get_mapped() and row.get_width() > 0)
    row.grab_focus()
    until(lambda: window.get_focus() is row)
    key(window, 0xFFBF)
    until(lambda: window.rename_dialog is not dialog and not window.rename_dialog.closed)
    dialog = window.rename_dialog
    dialog.entry.set_text("")
    assert dialog.confirm.get_sensitive()
    dialog.confirm.emit("clicked")
    until(
        lambda: (
            dialog.closed
            and any(m.title == "Budget review" and m.directory == folder for m in window.snapshot.meetings)
        )
    )
    assert read_metadata(folder)["title"] is None
    assert read_metadata(folder)["titleSource"] == "meeting-window"
    assert before == {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.name != "meeting.json"
    }
    live = next(m for m in window.snapshot.meetings if m.lifecycle == "recording")
    row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == live)
    popover = window._row_menu(row, 100, 20)
    until(popover.get_mapped)
    assert not window.lookup_action("rename").get_enabled()
    popover.popdown()
    window._fixture_activity = window._activity = {}
    window.monitor.capture_activity = lambda: {}
    window.close()
    app.comfort.close()
print(
    "PASS Rename: actual row/Meeting menus, F2, Return/Escape, automatic name reset, failure/retry and unchanged audio"
)
