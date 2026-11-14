#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D100 / D-1032 real GTK: Move to Trash into the Library's own trash, Undo, ☰ › Trash
(Restore, Delete permanently…, Empty Trash…, Return is Cancel) and ☰ › Help (F1, offline)."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
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

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.library_v2 import trash as storage  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402


def until(check, message="Trash GUI did not reach expected state"):
    context = GLib.MainContext.default()
    expired = False

    def expire(*_args):
        nonlocal expired
        expired = True
        return GLib.SOURCE_REMOVE

    guard = GLib.timeout_source_new_seconds(18)
    guard.set_callback(expire)
    guard.attach(context)
    try:
        while not check() and not expired:
            context.iteration(True)
        assert check(), message
    finally:
        guard.destroy()


def idle_for(seconds):
    """Run the main loop for `seconds` (a bounded wait: the assertion is that nothing happens)."""
    done = []
    GLib.timeout_add(int(seconds * 1000), lambda: done.append(True) and GLib.SOURCE_REMOVE)
    context = GLib.MainContext.default()
    while not done:
        context.iteration(True)


def frames(window, count=3):
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < count)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= count)


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


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def activate_menu_item(popover, label):
    until(popover.get_mapped)
    candidates = [
        child for child in widgets(popover) if child.__gtype__.name == "GtkModelButton" and label in labels(child)
    ]
    assert len(candidates) == 1, [
        (child.__gtype__.name, labels(child)) for child in widgets(popover) if "Button" in child.__gtype__.name
    ]
    item = candidates[0]
    assert item.get_mapped() and item.is_sensitive()
    # Activate the native menu item, including its close-before-action handler.
    assert item.activate()


def fingerprint(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def primary_menu(window):
    return next(
        w
        for w in widgets(window)
        if isinstance(w, Gtk.MenuButton) and w.get_mapped() and w.get_menu_model() and w.get_primary()
    )


def menu_items(model):
    """(label, action) in order, sections flattened, with a None between sections."""
    out = []
    for index in range(model.get_n_items()):
        section = model.get_item_link(index, Gio.MENU_LINK_SECTION)
        if section is not None:
            if out:
                out.append(None)
            out += menu_items(section)
            continue
        label = model.get_item_attribute_value(index, "label", None)
        action = model.get_item_attribute_value(index, "action", None)
        out.append((label.get_string() if label else None, action.get_string() if action else None))
    return out


def open_from_primary(window, label):
    button = primary_menu(window)
    button.popup()
    activate_menu_item(button.get_popover(), label)


def visible_alert(window):
    dialog = window.get_visible_dialog()
    return dialog if isinstance(dialog, Adw.AlertDialog) else None


def press_return(window, dialog):
    """Return in a confirmation (HARMONY §7: Cancel for 4 and 5). Return goes to the focused
    widget first (GtkButton's Return keybinding activates it), then to the window's
    `activate-default`. The focused widget must be the default response, Cancel."""
    default = dialog.get_default_widget()
    assert default is not None and "Cancel" in labels(default), "the default response is not Cancel"
    until(lambda: dialog.get_focus() is default, "Return would not reach the confirmation's Cancel")
    assert window.get_focus() is default
    frames(window)  # the dialog accepts a response once its present transition has run
    assert default.activate()  # what the focused button does on Return
    until(lambda: not dialog.get_mapped(), "Return did not cancel the confirmation")


def row_for(dialog, title):
    rows = [r for r in dialog.rows.values() if r.entry.title == title]
    assert len(rows) == 1, [r.entry.title for r in dialog.rows.values()]
    return rows[0]


def in_library(window, key):
    return any(m.key == key for m in window.snapshot.meetings)


def settle(controller):
    until(lambda: not controller.operations)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    library = root / "fixtures/main/root"
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(library),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK
        if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
        else Adw.ColorScheme.FORCE_LIGHT
    )

    def no_system_trash(*_args, **_kwargs):
        raise AssertionError("D100: nothing goes through the system Trash or the Trash portal")

    def no_network(*_args, **_kwargs):
        raise AssertionError("HARMONY §12: Help opens no URL")

    blockers = [
        patch.object(Gio.File, "trash", no_system_trash),
        patch.object(Gio.File, "trash_async", no_system_trash),
        patch.object(Gtk.UriLauncher, "launch", no_network),
        patch.object(Gio.AppInfo, "launch_default_for_uri", no_network),
        patch.object(Gio.AppInfo, "launch_default_for_uri_async", no_network),
    ]
    for blocker in blockers:
        blocker.start()

    app = CassiniApplication("com.gocassini.TrashTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending and window.get_width() == 1120 and window.get_height() == 720)
    controller = app.meeting_trash
    settle(controller)
    budget = next(m for m in window.snapshot.meetings if m.title == "Budget review")
    client = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    live = next(m for m in window.snapshot.meetings if m.lifecycle == "recording")
    assert not controller.allowed(live, window)
    budget_bytes = fingerprint(budget.directory)

    # ☰ menu: HARMONY §10 Phase 2 order, catalog words.
    items = menu_items(primary_menu(window).get_menu_model())
    assert items == [
        ("Import audio…", "app.open"),
        ("Export All as ZIP…", "win.export-zip"),
        None,
        ("Trash", "app.trash"),
        None,
        ("Preferences", "app.preferences"),
        ("Keyboard shortcuts", "app.shortcuts"),
        ("Help", "app.help"),
        ("_About Cassini talk", "app.about"),
    ], items
    assert app.get_accels_for_action("app.help") == ["F1"]

    # Every Move to Trash route moves the meeting whole into .cassini/trash at once; Undo restores it.
    outcomes = {}
    for surface in ("row", "meeting"):
        if surface == "row":
            row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == budget)
            popover = window._row_menu(row, 170, 20)
        else:
            window.open_meeting(budget)
            menu = window.meeting_page.more_menu
            button = next(
                w for w in widgets(window.meeting_page) if isinstance(w, Gtk.MenuButton) and w.get_menu_model() == menu
            )
            button.popup()
            popover = button.get_popover()
        activate_menu_item(popover, "Move to Trash")
        with suppress(AssertionError):
            until(lambda: controller.toast is not None and not budget.directory.exists())
        moved = not budget.directory.exists() and len(storage.entries(library)) == 1
        toast = controller.toast
        if toast:
            toast.emit("button-clicked")
            toast.dismiss()
        settle(controller)
        with suppress(AssertionError):
            until(lambda: in_library(window, budget.key))
        outcomes[surface] = bool(moved and budget.directory.exists() and in_library(window, budget.key))
        popover.popdown()
        if window.meeting_page:
            window.back_to_library()
    print("Move to Trash routes: " + json.dumps(outcomes), flush=True)
    assert all(outcomes.values()), outcomes
    assert fingerprint(budget.directory) == budget_bytes and not storage.entries(library)

    row = window.rows.get_first_child()
    while not hasattr(row, "meeting") or row.meeting.key != budget.key:
        row = row.get_next_sibling()
    popup = window._row_menu(row, 170, 20)
    until(popup.get_mapped)
    assert "Move to Trash" in labels(popup) and "Export…" in labels(popup)
    capture(window, "-library-menu")
    popup.popdown()
    until(lambda: not popup.get_parent())

    # Delete belongs to list/Meeting only, never text input.
    window.open_meeting(budget)
    entry = Gtk.Entry(text="keep this text")
    window.meeting_page.lines.append(entry)
    window.meeting_page.lines.set_visible(True)
    entry.grab_focus()
    until(window._text_focused)
    assert not window._trash_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
    assert budget.directory.exists() and not controller.operations
    window.meeting_page.lines.remove(entry)
    window.meeting_page.lines.set_visible(window.meeting_page.lines.get_first_child() is not None)
    window.back_to_library()

    # Delete key on the selected row: moved at once, 5 s Undo toast, Undo restores byte-identical.
    row = window.rows.get_first_child()
    while not hasattr(row, "meeting") or row.meeting.key != budget.key:
        row = row.get_next_sibling()
    window.rows.select_row(row)
    row.grab_focus()
    assert window._trash_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
    toast = controller.toast
    assert toast.get_timeout() == 5
    settle(controller)
    assert not budget.directory.exists()
    (entry_row,) = storage.entries(library)
    assert entry_row.title == "Budget review"
    assert fingerprint(library / ".cassini/trash" / entry_row.entry_id / budget.directory.name) == budget_bytes
    assert not in_library(window, budget.key)
    assert window.selected and window.selected.key != budget.key
    capture(window, "-undo")
    toast.emit("button-clicked")
    toast.dismiss()
    settle(controller)
    until(lambda: in_library(window, budget.key))
    assert fingerprint(budget.directory) == budget_bytes and window.selected.key == budget.key

    # Move two meetings and let the toasts close: they stay in the Trash.
    controller.request(budget, window)
    settle(controller)
    controller.request(client, window)
    settle(controller)
    controller.toast.dismiss()
    assert not budget.directory.exists() and not client.directory.exists()
    # Same-second deletions tie on deletedAtUtc; the contract breaks ties by entry id.
    assert sorted(e.title for e in storage.entries(library)) == ["Budget review", "Client call, Rossi & Partners"]

    # ☰ › Trash: rows newest first, title then subtitle · days left, retention note.
    open_from_primary(window, "Trash")
    until(lambda: app.trash_dialog is not None and app.trash_dialog.entries is not None)
    dialog = app.trash_dialog
    assert window.get_visible_dialog() is dialog and dialog.get_title() == "Trash"
    assert [dialog.list.get_row_at_index(i).entry.entry_id for i in range(2)] == [
        e.entry_id for e in storage.entries(library)
    ], "newest deletion first, as storage orders them"
    client_row = row_for(dialog, "Client call, Rossi & Partners")
    assert client_row.get_title() == GLib.markup_escape_text("Client call, Rossi & Partners")
    assert client_row.get_subtitle().endswith(" · 30 days left"), client_row.get_subtitle()
    assert "Meetings in the Trash are deleted permanently after 30 days." in labels(dialog)
    assert dialog.empty_button.is_sensitive() and dialog.empty_button.get_label() == "Empty Trash…"
    assert dialog.list.get_accessible_role() == Gtk.AccessibleRole.LIST
    capture(window, "-trash-view")

    # An open Trash with an unchanged list is idle: reading the entries never notifies the
    # listeners, so the view does not reload, re-render or refresh the Library in a loop.
    loads, renders, rescans = [], [], []
    real_render = dialog._render
    controller.entries = lambda done, real=controller.entries: (loads.append(True), real(done))[1]
    dialog._render = lambda: (renders.append(True), real_render())[1]
    window.monitor.refresh_now = lambda real=window.monitor.refresh_now: (rescans.append(True), real())[1]
    dialog.reload()
    until(lambda: renders)
    idle_for(1.5)
    assert (len(loads), len(renders), len(rescans)) == (1, 1, 0), "the open Trash kept reloading"
    del controller.entries, dialog._render, window.monitor.refresh_now

    # Restore (no confirmation): back in the Library, its toast in the view.
    client_row.restore_button.emit("clicked")
    until(lambda: client.directory.exists() and len(dialog.rows) == 1)
    assert visible_alert(window) is None
    assert "“Client call, Rossi & Partners” restored to the Library" in labels(dialog.toasts)
    until(lambda: in_library(window, client.key))

    # Delete permanently…: confirmation 4, Return and Escape are Cancel.
    budget_row = row_for(dialog, "Budget review")
    budget_row.grab_focus()
    assert dialog._row_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0), budget_row.entry)
    alert = dialog.confirmation
    until(lambda: alert.get_mapped())
    assert alert.get_heading() == "Delete “Budget review” permanently?"
    assert alert.get_body() == "Its audio and transcript are removed from this computer. This can’t be undone."
    assert alert.get_default_response() == "cancel" and alert.get_close_response() == "cancel"
    assert alert.get_response_label("cancel") == "Cancel" and alert.get_response_label("delete") == "Delete"
    assert alert.get_response_appearance("delete") == Adw.ResponseAppearance.DESTRUCTIVE
    assert "Cancel" in labels(alert.get_default_widget())
    capture(window, "-delete-confirm")
    press_return(window, alert)
    until(lambda: not alert.get_mapped())
    assert len(storage.entries(library)) == 1, "Return must not delete permanently"
    budget_row.delete_button.emit("clicked")
    alert = dialog.confirmation
    until(lambda: alert.get_mapped())
    alert.close()  # Escape: the close response, Cancel
    until(lambda: not alert.get_mapped())
    assert len(storage.entries(library)) == 1
    budget_row.delete_button.emit("clicked")
    alert = dialog.confirmation
    until(lambda: alert.get_mapped())
    alert.emit("response", "delete")
    until(lambda: not storage.entries(library) and dialog.stack.get_visible_child_name() == "empty")
    assert not (library / budget.directory.name).exists()
    assert not [p for p in (library / ".cassini/trash").iterdir()]
    assert not dialog.empty_button.is_sensitive() and "The Trash is empty." in labels(dialog)
    capture(window, "-trash-empty")

    # Empty Trash…: confirmation 5, Return is Cancel, then one purge per entry.
    dialog.close()
    until(lambda: app.trash_dialog is None)
    controller.request(client, window)
    settle(controller)
    controller.toast.dismiss()
    app.activate_action("trash", None)
    until(lambda: app.trash_dialog is not None and app.trash_dialog.entries)
    dialog = app.trash_dialog
    dialog.empty_button.emit("clicked")
    alert = dialog.confirmation
    until(lambda: alert.get_mapped())
    assert alert.get_heading() == "Empty the Trash?"
    assert alert.get_response_label("delete") == "Empty Trash"
    assert alert.get_default_response() == "cancel" and alert.get_close_response() == "cancel"
    press_return(window, alert)
    until(lambda: not alert.get_mapped())
    assert len(storage.entries(library)) == 1, "Return must not empty the Trash"
    dialog.empty_button.emit("clicked")
    alert = dialog.confirmation
    until(lambda: alert.get_mapped())
    alert.emit("response", "delete")
    until(lambda: not storage.entries(library) and dialog.stack.get_visible_child_name() == "empty")
    assert not client.directory.exists()
    dialog.close()
    until(lambda: app.trash_dialog is None)

    # ☰ › Help (F1): four catalog topics and Keyboard shortcuts; no URL is opened.
    open_from_primary(window, "Help")
    until(lambda: app.help_dialog is not None and app.help_dialog.get_mapped())
    help_dialog = app.help_dialog
    shown = labels(help_dialog)
    for title in (
        "Recording a meeting",
        "Where your meetings are",
        "Getting a meeting back",
        "Recognising voices",
    ):
        assert title in shown, (title, shown)
    assert (
        "Deleted meetings stay in the Trash for 30 days. Open Trash from the main menu, then choose Restore." in shown
    )
    assert help_dialog.get_title() == "Help"
    capture(window, "-help")
    help_dialog.close()
    until(lambda: app.help_dialog is None)
    app.activate_action("help", None)  # what F1 (its only accelerator) triggers
    until(lambda: app.help_dialog is not None and app.help_dialog.get_mapped())
    current = app.help_dialog
    app.activate_action("help", None)
    assert app.help_dialog is current, "repeated F1 must not stack Help dialogs"
    app.help_dialog.shortcuts_button.emit("clicked")
    until(lambda: app.help_dialog is None and app.shortcuts_dialog is not None and app.shortcuts_dialog.get_mapped())
    shortcut_labels = labels(app.shortcuts_dialog)
    assert "Help" in shortcut_labels and "Delete permanently…" in shortcut_labels, shortcut_labels
    app.shortcuts_dialog.close()
    until(lambda: app.shortcuts_dialog is None)

    assert app.meeting_trash.flush(lambda: None) is False, "no move or restore is left running"
    app.meeting_trash.executor.shutdown(wait=True)
    window.close()
    for blocker in blockers:
        blocker.stop()
print(
    "Trash GUI passed: in-Library Move to Trash, Undo, ☰ › Trash restore/delete permanently/empty "
    "(Return is Cancel), ☰ › Help (F1, offline)"
)
