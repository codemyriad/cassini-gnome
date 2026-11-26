#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D112 (L-87) Set date and time: typing hour/minute on an already-dated meeting enables and commits Save."""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
gi.require_version("Graphene", "1.0")
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import GdkX11, Gio, Graphene, Gtk  # noqa: E402
from painted import apply_screenshot_scheme, painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_store import read_metadata  # noqa: E402
from cassini_talk.strings import text  # noqa: E402


def until(check, message="Set date UI condition timed out"):
    try:
        dispatch_until(check, message)
    except AssertionError:
        dialog = globals().get("dialog")
        if dialog is not None:
            print(
                "Set date timeout:",
                dict(
                    closed=dialog.closed,
                    busy=dialog.busy,
                    failed=dialog.failed,
                    edited=dialog.edited,
                    hourText=dialog.hour.get_text(),
                    hourValue=dialog.hour.get_value(),
                    minuteText=dialog.minute.get_text(),
                    minuteValue=dialog.minute.get_value(),
                    confirmSensitive=dialog.confirm.get_sensitive(),
                    futureDone=dialog.future.done() if dialog.future else None,
                    futureError=str(dialog.future.exception()) if dialog.future and dialog.future.done() else None,
                ),
                flush=True,
            )
        raise


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def labels(widget):
    return [w.get_text() for w in widgets(widget) if isinstance(w, Gtk.Label)]


def open_set_date_from_row(window, meeting):
    row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == meeting)
    popover = window._row_menu(row, 100, 20)
    until(popover.get_mapped, "row menu did not map")
    target = text("meeting.setDateTimeEllipsis")
    buttons = [w for w in widgets(popover) if w.__gtype__.name == "GtkModelButton"]
    item = next(w for w in buttons if target in labels(w))
    until(lambda: item.get_mapped() and item.get_width() > 0, "Set date menu item did not allocate")
    assert item.is_sensitive()
    assert item.activate()
    until(lambda: window.set_date_dialog is not None and not window.set_date_dialog.closed, "dialog did not open")
    dlg = window.set_date_dialog
    until(lambda: dlg.hour.get_mapped() and dlg.hour.get_width() > 0, "hour spin did not allocate")
    until(lambda: dlg.confirm.get_mapped() and dlg.confirm.get_width() > 0, "Save button did not allocate")
    return dlg


def send_keys(window, symbols):
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    window.get_display().sync()
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True)
    until(window.is_active, "window did not activate")
    x11, xtst = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(None)
    assert display
    try:
        for symbol in symbols:
            code = x11.XKeysymToKeycode(display, symbol)
            assert code, f"no keycode for keysym {symbol:#x}"
            xtst.XTestFakeKeyEvent(display, code, True, 0)
            xtst.XTestFakeKeyEvent(display, code, False, 0)
        x11.XSync(display, False)
    finally:
        x11.XCloseDisplay(display)


def replace_spin_text(window, spin, digits):
    delegate = spin.get_delegate()
    spin.grab_focus()
    until(lambda: window.get_focus() is delegate, "spin did not take focus")
    current = spin.get_text()
    spin.set_position(len(current))
    # BackSpace clears existing digits, then type each digit with real X11 key events.
    symbols = [0xFF08] * max(1, len(current)) + [ord(ch) for ch in digits]
    send_keys(window, symbols)
    until(lambda: spin.get_text() == digits, f"spin text did not become {digits!r} (got {spin.get_text()!r})")


def click_widget(window, widget):
    ok, point = widget.compute_point(window, Graphene.Point().init(widget.get_width() / 2, widget.get_height() / 2))
    assert ok, "could not map widget coordinates to window"
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    window.get_display().sync()
    subprocess.run(
        ["xdotool", "mousemove", "--sync", "--window", str(xid), str(int(point.x)), str(int(point.y)), "click", "1"],
        check=True,
    )


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
    apply_screenshot_scheme()
    app = CassiniApplication("com.gocassini.SetDateTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending, "library did not settle")
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True)
    until(window.is_active, "window did not activate")

    # 1) Already-dated meeting: typing an invalid hour keeps Save disabled; typing a valid hour
    # immediately enables Save while focus is still in the spin button, and clicking Save persists it.
    chosen = next(m for m in window.snapshot.meetings if m.title == "Budget review")
    assert chosen.started_at_ms is not None
    folder = chosen.directory
    dialog = open_set_date_from_row(window, chosen)
    assert not dialog.confirm.get_sensitive(), "Save should start disabled for an already-dated meeting"
    original_hour = int(dialog.hour.get_value())
    original_minute = int(dialog.minute.get_value())
    new_hour = (original_hour + 2) % 24
    new_hour_text = str(new_hour)

    replace_spin_text(window, dialog.hour, "99")
    assert not dialog.confirm.get_sensitive(), "out-of-range hour must keep Save disabled"
    replace_spin_text(window, dialog.hour, "")
    assert not dialog.confirm.get_sensitive(), "empty hour must keep Save disabled"
    replace_spin_text(window, dialog.hour, new_hour_text)
    assert int(dialog.hour.get_value()) == original_hour, "typed hour is still uncommitted before focus leaves"
    assert dialog.confirm.get_sensitive(), "valid typed hour must immediately enable Save"

    # Capture the open dialog screenshot for ux-shots manifest parity (light and dark).
    ticks = []
    window.add_tick_callback(lambda *_, ticks=ticks: (ticks.append(True), len(ticks) < 3)[1])
    window.queue_draw()
    until(lambda ticks=ticks: len(ticks) >= 3, "frame ticks did not advance")
    node = painted_node(window)
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])

    # Immediately click Save with a real X11 mouse click without tabbing out first.
    click_widget(window, dialog.confirm)
    expected_time = f"T{new_hour:02d}:{original_minute:02d}:00"
    until(
        lambda: dialog.closed and expected_time in (read_metadata(folder).get("startedAt") or ""),
        "clicking Save after typing hour did not persist the typed hour",
    )
    assert read_metadata(folder)["startedAtSource"] == "user"
    until(
        lambda: any(
            m.directory == folder and expected_time in (m.started_at_user or "") for m in window.snapshot.meetings
        ),
        "saved hour did not propagate to Library snapshot",
    )

    # 2) Reopen the now user-dated meeting: type a new minute without leaving focus, verify
    # reverting/invalidating disables Save, then trigger Save while focus stays in the minute spin.
    chosen = next(m for m in window.snapshot.meetings if m.directory == folder)
    dialog = open_set_date_from_row(window, chosen)
    assert not dialog.confirm.get_sensitive()
    new_minute = (original_minute + 11) % 60
    replace_spin_text(window, dialog.minute, "60")
    assert not dialog.confirm.get_sensitive(), "out-of-range minute must keep Save disabled"
    replace_spin_text(window, dialog.minute, f"{original_minute:02d}")
    assert not dialog.confirm.get_sensitive(), "unchanged minute on user-dated meeting must keep Save disabled"
    replace_spin_text(window, dialog.minute, str(new_minute))
    assert int(dialog.minute.get_value()) == original_minute, "typed minute is still uncommitted"
    assert dialog.confirm.get_sensitive(), "valid typed minute must immediately enable Save"
    dialog.confirm.emit("clicked")
    expected_time_2 = f"T{new_hour:02d}:{new_minute:02d}:00"
    until(
        lambda: dialog.closed and expected_time_2 in (read_metadata(folder).get("startedAt") or ""),
        "activating Save with uncommitted minute did not commit and persist the typed minute",
    )

    window._fixture_activity = window._activity = {}
    window.monitor.capture_activity = lambda: {}
    window.close()
    app.comfort.close()

print("PASS Set date: typing hour/minute enables Save immediately, rejects invalid text, and commits on Save")
