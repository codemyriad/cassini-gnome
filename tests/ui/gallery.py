#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1157 QA gallery: one main screen of the real app, in one scheme, size and text size.

Not a test. build-aux/ux-gallery.sh runs this once per screen, scheme, window
size and text size, against the shared Library fixtures, and names the files
gnome-NN-screen[-narrow|-large-text]-light|dark.png. The window is grabbed from
the private Xvfb root so menus (their own surfaces) are in the picture; Xvfb
has no compositor, so there is no shadow and no rounded corner to trim.

Environment: CASSINI_GALLERY_SCREEN (a key of SCREENS), CASSINI_GALLERY_SIZE
(wide or narrow), CASSINI_GALLERY_LARGE_TEXT=1 (GNOME Large Text, scale 1.25),
CASSINI_SCREENSHOT_SCHEME=dark, CASSINI_SCREENSHOT_OUT (any file in the output
folder).
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
gi.require_version("GdkX11", "4.0")
from gi.repository import Adw, GdkX11, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402

SIZES = {"wide": (1130, 730), "narrow": (370, 790)}
NOW = "1793107080000"
SCREEN = os.environ["CASSINI_GALLERY_SCREEN"]
SIZE = os.environ.get("CASSINI_GALLERY_SIZE", "wide")
LARGE = os.environ.get("CASSINI_GALLERY_LARGE_TEXT") == "1"
DARK = os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
OUT = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).parent


def until(check, message="Gallery screen did not settle", seconds=20):
    end = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    assert check(), message


def idle(seconds):
    end = time.monotonic() + seconds
    until(lambda: time.monotonic() >= end)


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def grab(window):
    """The window's own pixels from the Xvfb root, popovers included."""
    from PIL import ImageGrab

    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XTranslateCoordinates.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
    x11.XTranslateCoordinates.argtypes += [ctypes.POINTER(ctypes.c_int)] * 2 + [ctypes.POINTER(ctypes.c_ulong)]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(None)
    assert display
    x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
    try:
        xid = GdkX11.X11Surface.get_xid(window.get_surface())
        root = x11.XDefaultRootWindow(display)
        assert x11.XTranslateCoordinates(
            display, xid, root, 0, 0, ctypes.byref(x), ctypes.byref(y), ctypes.byref(child)
        )
        dx, dy = window.get_surface_transform()
        left, top = x.value + int(dx), y.value + int(dy)
        box = (left, top, left + window.get_width(), top + window.get_height())
        return ImageGrab.grab(bbox=box, xdisplay=os.environ["DISPLAY"])
    finally:
        x11.XCloseDisplay(display)


def name(stem):
    variant = "-narrow" if SIZE == "narrow" else ""
    variant += "-large-text" if LARGE else ""
    return f"gnome-{stem}{variant}-{'dark' if DARK else 'light'}.png"


SHOTS = []


def shoot(window, stem, settle=0.6):
    """Let animations, idle loads and the paint finish, then save the window."""
    idle(settle)
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 4)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= 4)
    image = grab(window)
    target = OUT / name(stem)
    image.save(target)
    SHOTS.append(dict(file=target.name, width=image.width, height=image.height))
    print(f"Saved {target}", flush=True)


def scrolled_pages(widget):
    """Vertical scrollers in view whose content is taller than their viewport."""
    for child in widgets(widget):
        if isinstance(child, Gtk.ScrolledWindow) and child.get_mapped():
            adjustment = child.get_vadjustment()
            if adjustment.get_upper() - adjustment.get_page_size() > 8:
                yield adjustment


def shoot_with_scroll(window, stem, container):
    """A tall page: its top, then its end, so every row of it is seen."""
    shoot(window, stem)
    adjustments = list(scrolled_pages(container))
    if adjustments:
        for adjustment in adjustments:
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        shoot(window, stem + "-end")


def quiet_player(window):
    """Fixture audio plays into a fakesink, never into the host."""
    from cassini_talk.folder_player import FolderPlayer

    window.player.shutdown()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player = FolderPlayer(audio_sink=sink)
    window._connect_player()


def meeting(window, title=None, *, test=None):
    found = [m for m in window.snapshot.meetings if (m.title == title if title else test(m))]
    assert len(found) == 1, [m.title for m in window.snapshot.meetings]
    return found[0]


def open_meeting(window, title=None, *, test=None, seek=None):
    chosen = meeting(window, title, test=test)
    window.open_meeting(chosen)
    if seek is not None:
        window.player.seek(seek)
    until(lambda: window.meeting_page is not None and window.meeting_page.list.ready)
    return chosen


def models_on(app):
    """Render the accepted On state of the optional models, as the boards do."""
    if app.diarization_models:
        until(
            lambda: (
                not app.diarization_models.snapshot()["busy"]
                and not (app.voice_models and app.voice_models.snapshot()["busy"])
            )
        )
        app.diarization_models._change(downloaded=True, ready=True, busy=False, percent=100)
        if app.voice_models:
            app.voice_models._change(downloaded=True, ready=True, busy=False, percent=100)


def seed_voices(app, window):
    from cassini_talk.voice_board import seed

    chosen = meeting(window, "Client call, Rossi & Partners")
    window.monitor._executor.submit(seed, app, chosen).result(timeout=5)


def open_menu(window, button):
    button.popup()
    popover = button.get_popover()
    until(popover.get_mapped)
    return popover


def library(app, window):
    shoot(window, "01-library")


def meeting_screen(app, window):
    open_meeting(window, "Budget review", seek=192000)
    shoot(window, "02-meeting")


def search(app, window):
    window.toggle_search()
    window.search_page.entry.set_text("budget")
    window.search_page.choose_speaker("Marta")
    until(lambda: not window.search_page.pending and len(window.search_page.hits) == 4)
    if SIZE == "narrow":
        # Choosing a place there opens it over the list; show the list itself.
        shoot(window, "03-search")
        return
    index = next(i for i, hit in enumerate(window.search_page.hits) if hit.position_ms == 872000)
    window.search_page.selection.set_selected(index)
    shoot(window, "03-search")


def live_recording(app, window):
    from cassini_talk.recording_details import recording_observation

    chosen = meeting(window, test=lambda m: m.lifecycle == "recording")
    observed = window.monitor._executor.submit(recording_observation, chosen, freshness_seconds=None).result(timeout=5)
    app.comfort.states[chosen.key] = dict(observed, silent=observed["levels"][0] == 0, line=None)
    window.open_meeting(chosen)
    window._update_live()
    return chosen


def recording(app, window):
    live_recording(app, window)
    shoot(window, "04-recording")


def transcribing(app, window):
    open_meeting(window, "Client call, Rossi & Partners")
    shoot(window, "05-transcribing")


def speakers(app, window):
    seed_voices(app, window)
    open_meeting(window, "Client call, Rossi & Partners")
    window.meeting_page.name_speakers("guest")
    dialog = window.meeting_page.speakers_dialog
    dialog.entry.set_text("Giulia")
    until(lambda: dialog.voices_loaded)
    shoot(window, "06-name-the-speakers")


PREFERENCE_PAGES = (("recording", "recording"), ("transcription", "transcription"), ("library", "library"))
PREFERENCE_PAGES += (("browserCompanion", "companion"),)


def preferences(app, window):
    seed_voices(app, window)
    models_on(app)
    app.on_preferences()
    dialog = app.settings_dialog
    until(lambda: dialog._future is None and dialog.library_stats is not None)
    for page, stem in PREFERENCE_PAGES:
        dialog.set_visible_page_name(page)
        until(lambda: dialog._future is None)
        shoot_with_scroll(window, f"07-preferences-{stem}", dialog.pages[page])


def saved_voices(app, window):
    seed_voices(app, window)
    models_on(app)
    app.on_preferences()
    dialog = app.settings_dialog
    dialog.set_visible_page_name("transcription")
    dialog._show_voices()
    until(lambda: not dialog.saved_voices_page._loading and dialog._future is None)
    shoot_with_scroll(window, "08-saved-voices", dialog)


def recently_deleted(app, window):
    controller = app.meeting_trash
    for title in ("Planning, Q4", "Design crit"):
        controller.request(meeting(window, title), window)
        until(lambda: not controller.operations)
        if controller.toast:
            controller.toast.dismiss()
    until(lambda: not controller.operations and not window._pending)
    dialog = app.on_trash()
    until(lambda: len(dialog.rows) == 2)
    shoot(window, "09-recently-deleted")


def export(app, window):
    open_meeting(window, "Budget review")
    window.show_export()
    shoot(window, "10-export")


def received(app, window):
    open_meeting(window, test=lambda m: m.directory.name.startswith("Received-"), seek=2000)
    shoot(window, "11-received")


def first_run(app, window):
    shoot(window, "12-first-run")


def what_is_recorded(app, window):
    live_recording(app, window)
    window.recording_page.change_sources.emit("clicked")
    shoot(window, "12-what-is-recorded")


def meeting_menu(app, window):
    open_meeting(window, "Budget review")
    menu = window.meeting_page.more_menu
    button = next(
        w for w in widgets(window.meeting_page) if isinstance(w, Gtk.MenuButton) and w.get_menu_model() == menu
    )
    open_menu(window, button)
    shoot(window, "14-meeting-menu")


def main_menu(app, window):
    button = next(
        w
        for w in widgets(window)
        if isinstance(w, Gtk.MenuButton) and w.get_mapped() and w.get_menu_model() and w.get_primary()
    )
    open_menu(window, button)
    shoot(window, "15-main-menu")


def library_row_menu(app, window):
    chosen = meeting(window, "Budget review")
    row = next(w for w in widgets(window.rows) if getattr(w, "meeting", None) == chosen)
    popover = window._row_menu(row, 170, 20)
    until(popover.get_mapped)
    shoot(window, "16-library-row-menu")


def recording_details(app, window):
    open_meeting(window, "Budget review")
    window.show_recording_details()
    shoot(window, "17-recording-details")


def keyboard_shortcuts(app, window):
    app.on_shortcuts()
    shoot(window, "18-keyboard-shortcuts")


def help_screen(app, window):
    app.on_help()
    shoot(window, "19-help")


def about(app, window):
    app.on_about()
    shoot(window, "20-about")


def activate_menu_item(popover, label):
    def labels(widget):
        return [w.get_text() for w in widgets(widget) if isinstance(w, Gtk.Label)]

    items = [w for w in widgets(popover) if w.__gtype__.name == "GtkModelButton" and label in labels(w)]
    assert len(items) == 1, label
    assert items[0].activate()


def rename(app, window):
    """Rename through the meeting menu, as a person does."""
    from cassini_talk.strings import text

    open_meeting(window, "Budget review")
    menu = window.meeting_page.more_menu
    button = next(
        w for w in widgets(window.meeting_page) if isinstance(w, Gtk.MenuButton) and w.get_menu_model() == menu
    )
    activate_menu_item(open_menu(window, button), text("meeting.renameEllipsis"))
    until(lambda: window.rename_dialog is not None, "Meeting menu › Rename… opened no dialog")
    shoot(window, "21-rename")


# screen -> (fixture case, function)
SCREENS = {
    "library": ("main", library),
    "meeting": ("main", meeting_screen),
    "search": ("main", search),
    "recording": ("recording", recording),
    "transcribing": ("main", transcribing),
    "speakers": ("main", speakers),
    "preferences": ("main", preferences),
    "saved-voices": ("main", saved_voices),
    "recently-deleted": ("main", recently_deleted),
    "export": ("main", export),
    "received": ("main", received),
    "first-run": ("empty", first_run),
    "what-is-recorded": ("recording", what_is_recorded),
    "meeting-menu": ("main", meeting_menu),
    "main-menu": ("main", main_menu),
    "library-row-menu": ("main", library_row_menu),
    "recording-details": ("main", recording_details),
    "keyboard-shortcuts": ("main", keyboard_shortcuts),
    "help": ("main", help_screen),
    "about": ("main", about),
    "rename": ("main", rename),
}


def main():
    case, screen = SCREENS[SCREEN]
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        schemas = tmp / "schemas"
        schemas.mkdir()
        shutil.copyfile(
            ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
        )
        subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
        fixtures = tmp / "fixtures"
        subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(fixtures)], check=True)
        subprocess.run([sys.executable, str(ROOT / "tests/ui/recording-fixture.py"), str(fixtures)], check=True)
        # The usual place, so paths on screen read like a person's own (D-1157).
        library_dir = Path.home() / "Music/Cassini"
        library_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(fixtures / case / "root", library_dir)  # keeps the sparse WAVs sparse
        os.environ.update(
            GSETTINGS_SCHEMA_DIR=str(schemas),
            CASSINI_LIBRARY_DIR=str(library_dir),
            CASSINI_SCREENSHOT_ACTIVITY=str(fixtures / case / "activity.json"),
            CASSINI_SCREENSHOT_NOW=NOW,
        )
        settings = Gtk.Settings.get_default()
        settings.props.gtk_enable_animations = False
        settings.props.gtk_cursor_blink = False
        settings.props.gtk_font_name = "Cantarell 11"
        if LARGE:
            # GNOME Large Text is text-scaling-factor 1.25, which GTK reads as Xft DPI.
            settings.props.gtk_xft_dpi = int(96 * 1.25 * 1024)
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if DARK else Adw.ColorScheme.FORCE_LIGHT
        )
        version = re.search(r"version: '([^']+)'", (ROOT / "meson.build").read_text()).group(1)
        app = CassiniApplication("com.gocassini.GalleryTest", version, "development")
        app.register(None)
        app.activate()
        window = app.get_active_window()
        width, height = SIZES[SIZE]
        window.set_default_size(width, height)
        until(lambda: not window._pending and window.get_width() > 0)
        quiet_player(window)
        screen(app, window)
        (OUT / f"{SCREEN}-{SIZE}{'-large-text' if LARGE else ''}-{'dark' if DARK else 'light'}.json").write_text(
            json.dumps(
                dict(
                    screen=SCREEN,
                    size=SIZE,
                    defaultSize=[width, height],
                    largeText=LARGE,
                    dark=DARK,
                    gtk=[Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()],
                    adwaita=[Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()],
                    scale=window.get_scale_factor(),
                    xftDpi=settings.props.gtk_xft_dpi / 1024,
                    shots=SHOTS,
                ),
                indent=2,
            )
            + "\n"
        )
        os._exit(0)  # The capture is done; skip teardown of dialogs and players.


main()
