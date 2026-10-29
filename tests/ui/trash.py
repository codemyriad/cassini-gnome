#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D29 real native menus/toast, exact deferred IO and failure/quit behaviour."""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
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
gi.require_version("GdkX11", "4.0")
from gi.repository import Adw, Gdk, GdkX11, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import meeting_trash as module  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402


def until(check, timeout=8):
    end = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    assert check(), "Trash GUI did not reach expected state"


def capture(window, suffix):
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"])
    target.parent.mkdir(parents=True, exist_ok=True)
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 3)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= 3)
    # Popovers have their own native surface; WidgetPaintable omits them.
    # Read pixels only from the private Xvfb root, including those surfaces.
    from PIL import ImageGrab

    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(None)
    assert display
    x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
    try:
        assert x11.XTranslateCoordinates(
            display,
            GdkX11.X11Surface.get_xid(window.get_surface()),
            x11.XDefaultRootWindow(display),
            0,
            0,
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(child),
        )
        dx, dy = window.get_surface_transform()
        left, top = x.value + int(dx), y.value + int(dy)
        result = ImageGrab.grab(
            bbox=(left, top, left + window.get_width(), top + window.get_height()), xdisplay=os.environ["DISPLAY"]
        )
        assert result.size == (1120, 720)
        result.save(target.with_name(target.stem + suffix + ".png"))
    finally:
        x11.XCloseDisplay(display)


def labels(widget):
    result = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        result += labels(child)
        child = child.get_next_sibling()
    return result


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK
        if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
        else Adw.ColorScheme.FORCE_LIGHT
    )
    app = CassiniApplication("com.gocassini.TrashTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending and window.get_width() == 1120 and window.get_height() == 720)
    controller = app.meeting_trash
    budget = next(m for m in window.snapshot.meetings if m.title == "Budget review")
    client = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    live = next(m for m in window.snapshot.meetings if m.lifecycle == "recording")
    assert not controller.allowed(live, window)
    row = window.rows.get_first_child()
    while not hasattr(row, "meeting") or row.meeting.key != budget.key:
        row = row.get_next_sibling()
    popup = window._row_menu(row, 170, 20)
    until(popup.get_mapped)
    assert window.selected.key == budget.key
    assert "Move to Trash" in labels(popup) and "Export…" in labels(popup)
    capture(window, "-library-menu")
    popup.popdown()
    until(lambda: not popup.get_parent())

    # Delete belongs to list/Meeting only, never text input or Search.
    entry = Gtk.Entry(text="keep this text")
    window.preview.append(entry)
    entry.grab_focus()
    until(window._text_focused)
    assert not window._trash_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
    assert budget.directory.exists() and not controller.operations
    window.preview.remove(entry)
    window.open_meeting(budget)
    assert controller.allowed(budget, window)
    menu = window.meeting_page.more_menu
    section = menu.get_item_link(menu.get_n_items() - 1, Gio.MENU_LINK_SECTION)
    assert section.get_item_attribute_value(0, "label", None).get_string() == "Move to Trash"

    def widgets(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from widgets(child)
            child = child.get_next_sibling()

    more = next(w for w in widgets(window.meeting_page) if isinstance(w, Gtk.MenuButton) and w.get_menu_model() == menu)
    more.popup()
    until(more.get_popover().get_mapped)
    capture(window, "-meeting-menu")
    more.popdown()
    window.back_to_library()

    calls, fail = [], []
    destination = root / "private-trash"
    destination.mkdir()

    def native(path):
        calls.append((path, time.monotonic()))
        if fail:
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), "generated unsupported drive", Gio.IOErrorEnum.NOT_SUPPORTED
            )
        path.rename(destination / path.name)
        return True

    original = module.trash_owned
    with patch.object(module, "trash_owned", side_effect=lambda path, **kwargs: original(path, trash=native, **kwargs)):
        row = window.rows.get_first_child()
        while not hasattr(row, "meeting") or row.meeting.key != budget.key:
            row = row.get_next_sibling()
        window.rows.select_row(row)
        row.grab_focus()
        assert window._trash_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
        toast = controller.toast
        assert toast.get_timeout() == 5
        assert budget.directory.exists() and not calls
        assert all(m.key != budget.key for m in window.snapshot.meetings)
        assert window.selected and window.selected.key != budget.key
        capture(window, "-undo")
        toast.emit("button-clicked")
        toast.dismiss()
        until(lambda: any(m.key == budget.key for m in window.snapshot.meetings))
        assert budget.directory.exists() and not calls and window.selected.key == budget.key

        # Real five-second AdwToast timeout, no manual dismissal and no fixed sleep.
        start = time.monotonic()
        controller.request(budget, window)
        until(lambda: not controller.operations)
        assert len(calls) == 1 and calls[0][1] - start >= 4.5
        assert not budget.directory.exists()
        until(lambda: budget.directory in controller.absent)
        (destination / budget.directory.name).rename(budget.directory)
        window.monitor.refresh_now()
        until(lambda: any(m.key == budget.key for m in window.snapshot.meetings))

        # Replacement commits the old toast; Undo only restores the newest one.
        controller.request(budget, window)
        controller.request(client, window)
        second = controller.toast
        until(lambda: not budget.directory.exists())
        assert client.directory.exists()
        second.emit("button-clicked")
        second.dismiss()
        until(lambda: not controller.operations)
        assert client.directory.exists()

        # Keep recording hides windows directly: an invisible toast must not
        # leave its pending removal stalled indefinitely.
        controller.request(client, window)
        window.set_visible(False)
        until(lambda: not controller.operations)
        assert not client.directory.exists() and not app._quitting
        window.present()
        until(lambda: client.directory in controller.absent)
        (destination / client.directory.name).rename(client.directory)
        window.monitor.refresh_now()
        until(lambda: any(m.key == client.key for m in window.snapshot.meetings))

        # Failure restores selection and one published warning/action.
        fail.append(True)
        controller.request(client, window)
        controller.toast.dismiss()
        until(lambda: not controller.operations)
        assert client.directory.exists() and window.selected.key == client.key
        assert (
            "This meeting’s drive has no Trash, so Cassini can’t move it there. It is still in your Library."
            in labels(window.preview)
        )
        capture(window, "-unsupported")
        fail.clear()

        # Last-row removal shows first run; pending Undo still works there.
        parked = root / "other-fixture-folders"
        parked.mkdir()
        for folder in client.directory.parent.iterdir():
            if folder.is_dir() and folder != client.directory:
                folder.rename(parked / folder.name)
        window.monitor.refresh_now()
        until(lambda: len(window.snapshot.meetings) == 1)
        controller.request(client, window)
        assert window.root_box.get_visible_child_name() == "empty"
        last = controller.toast
        last.emit("button-clicked")
        last.dismiss()
        until(lambda: len(window.snapshot.meetings) == 1)
        assert window.selected.key == client.key
        # Quit commits a pending toast and keeps the GTK loop alive for its reply.
        controller.request(client, window)
        assert client.directory.exists()
        app.on_quit()
        until(lambda: not app.get_windows())
        assert not client.directory.exists() and not controller.operations
    app.meeting_trash.executor.shutdown(wait=True)
print("Trash GUI passed: menus, text-safe Delete, Undo, five-second timeout, restore, replacement, failure and quit")
