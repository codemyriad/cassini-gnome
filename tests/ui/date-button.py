#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1177: the date in the meeting header opens Set date and time…; the rest of the line stays plain text."""

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
from gi.repository import GdkX11, Gio, Graphene, Gtk, Pango  # noqa: E402
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


def point_at(window, widget, x=None, y=None):
    ok, point = widget.compute_point(
        window,
        Graphene.Point().init(widget.get_width() / 2 if x is None else x, widget.get_height() / 2 if y is None else y),
    )
    assert ok, "could not map widget coordinates to window"
    return int(point.x), int(point.y)


def pointer(window, widget, *, click=False, x=None, y=None):
    px, py = point_at(window, widget, x, y)
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    window.get_display().sync()
    command = ["xdotool", "mousemove", "--sync", "--window", str(xid), str(px), str(py)]
    subprocess.run(command + (["click", "1"] if click else []), check=True)


class Releases:
    """Counts primary releases a widget receives (bubble phase, never claimed)."""

    def __init__(self, widget):
        self.count = 0
        gesture = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.BUBBLE)
        gesture.connect("released", self._released)
        widget.add_controller(gesture)

    def _released(self, *_args):
        self.count += 1


def click_plain(window, label, x, y, message):
    """Click the line's plain text and prove GTK dispatched that release before checking no dialog opened.

    The release reaches the label, not the date button. until() checks between
    main-loop iterations, so once the label has counted it GTK has finished
    dispatching the event; show_set_date presents the dialog synchronously from
    a button's release, so it would already be open.
    """
    on_label = Releases(label)
    pointer(window, label, click=True, x=x, y=y)
    until(lambda: on_label.count, f"{message}: the click did not reach the line's text")
    assert not dialog_open(window), message


def dialog_open(window):
    return window.set_date_dialog is not None and not window.set_date_dialog.closed


def wait_dialog(window, how):
    until(lambda: dialog_open(window), f"{how} did not open Set date and time")
    dlg = window.set_date_dialog
    until(lambda: dlg.confirm.get_mapped() and dlg.confirm.get_width() > 0, "Save did not allocate")
    return dlg


def close_with_escape(window, dlg):
    send_keys(window, [0xFF1B])  # Escape
    until(lambda: dlg.closed, "Esc did not close Set date and time")


def open_page(window, test):
    chosen = next(m for m in window.snapshot.meetings if test(m))
    window.open_meeting(chosen)
    until(lambda: window.meeting_page is not None and window.meeting_page.meeting.key == chosen.key)
    page = window.meeting_page
    until(lambda: page.date_button.get_mapped() and page.date_button.get_width() > 0, "date did not allocate")
    return chosen, page


def colour(widget):
    c = widget.get_color()
    return round(c.red, 3), round(c.green, 3), round(c.blue, 3)


def check_line(window, page, meeting):
    expected = page.compact_subtitle.get_label()
    date = page.date_button.get_child().get_label()
    assert page.subtitle_label.get_label() == expected, (page.subtitle_label.get_label(), expected)
    assert expected.startswith(date) and page.subtitle_line.clickable
    assert page.date_button.get_tooltip_text() == text("meeting.setDateTimeEllipsis")
    # Drawn as the line's text: the same grey as the rest, never the accent; regular weight.
    assert colour(page.date_button.get_child()) == colour(page.subtitle_label), "date is not the line's grey"
    assert page.date_button.get_cursor().get_name() == "pointer"
    # The text sits where the plain label's did: flush with the title.
    ok, at = page.date_button.get_child().compute_point(page.title_label, Graphene.Point().init(0, 0))
    assert ok and abs(at.x) <= 1, f"date text moved {at.x}px from the title's edge"
    ok, line = page.date_button.get_child().compute_point(page.subtitle_label, Graphene.Point().init(0, 0))
    x, y = page.subtitle_label.get_layout_offsets()
    assert ok and abs(line.x - x) <= 1 and abs(line.y - y) <= 1, "the date button is not over the date"
    return date


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
    app = CassiniApplication("com.gocassini.DateButtonTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending, "library did not settle")
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True)
    until(window.is_active, "window did not activate")

    # 1) A known date: hover shows the flat button's highlight; a click opens the dialog prefilled.
    chosen, page = open_page(window, lambda m: m.title == "Budget review")
    assert chosen.started_at_ms is not None
    before = read_metadata(chosen.directory)
    date = check_line(window, page, chosen)
    rest_x = page.date_button.get_width() + 40  # on "52 min"
    pointer(window, page.subtitle_label, x=rest_x)
    # GTK sets PRELIGHT along the hovered chain while it dispatches the motion.
    until(lambda: page.subtitle_label.get_state_flags() & Gtk.StateFlags.PRELIGHT, "the length did not take hover")
    assert not page.date_button.get_state_flags() & Gtk.StateFlags.PRELIGHT
    pointer(window, page.date_button)
    until(lambda: page.date_button.get_state_flags() & Gtk.StateFlags.PRELIGHT, "hover did not highlight the date")
    node = painted_node(window)
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
    pointer(window, page.date_button, click=True)
    dlg = wait_dialog(window, "Clicking the date")
    assert dlg.meeting.key == chosen.key
    shown = dlg.calendar.get_date()
    from cassini_talk import ux_formats as fmt

    started = fmt._local(chosen.started_at_ms, window.tz)
    assert (shown.get_year(), shown.get_month(), shown.get_day_of_month()) == (
        started.year,
        started.month,
        started.day,
    ), "known date was not prefilled"
    assert (int(dlg.hour.get_value()), int(dlg.minute.get_value())) == (started.hour, started.minute)
    close_with_escape(window, dlg)
    assert read_metadata(chosen.directory) == before, "Esc changed the meeting"

    # 2) Keyboard: focus the date and press Enter, then Space.
    for key in (0xFF0D, 0x20):
        page.date_button.grab_focus()
        until(lambda: window.get_focus() is page.date_button, "date did not take focus")
        send_keys(window, [key])
        close_with_escape(window, wait_dialog(window, f"Key {key:#x} on the date"))

    # 3) The length and "Imported" do nothing on click.
    click_plain(window, page.subtitle_label, rest_x, None, "clicking the length opened Set date and time")

    # 4) Where Set date and time… is unavailable the date is plain text with no hover.
    action = window.lookup_action("set-date")
    action.set_enabled(False)
    until(lambda: not page.subtitle_line.clickable, "date stayed a button")
    assert page.subtitle_label.get_attributes() is None or not page.subtitle_label.get_attributes().get_attributes()
    assert page.subtitle_label.get_label().startswith(date)
    action.set_enabled(True)
    until(lambda: page.date_button.get_visible(), "date did not become a button again")

    # 5) "Date unknown": neutral grey, and the dialog opens with its usual prefill.
    chosen, page = open_page(window, lambda m: m.directory.name.startswith("Received-"))
    assert chosen.started_at_ms is None
    assert check_line(window, page, chosen) == text("library.group.unknownDate")
    pointer(window, page.date_button)
    until(lambda: page.date_button.get_state_flags() & Gtk.StateFlags.PRELIGHT, "hover did not highlight")
    pointer(window, page.date_button, click=True)
    close_with_escape(window, wait_dialog(window, "Clicking Date unknown"))

    # 6) The compact header stays plain text.
    assert isinstance(page.compact_subtitle, Gtk.Label)

    # 7) Narrow: the line still fits the header, wrapping when it must.
    window.set_default_size(360, 780)
    line = page.subtitle_line

    def narrowed():
        # The header is laid out for the new width: the line fills it and wraps.
        return (
            window.get_width() <= 380
            and 0 < line.get_width() < 360
            and line.get_width() == page.subtitle_label.get_width()
            and page.subtitle_label.get_layout().get_width() == line.get_width() * Pango.SCALE
        )

    until(narrowed, "the date line did not narrow")
    ok, end = line.compute_point(page.full_header, Graphene.Point().init(line.get_width(), 0))
    assert ok and end.x <= page.full_header.get_width(), "the date line overflows the narrow header"
    # Still one label wrapping word by word ("… Transcribed / on another device"), the button on its date.
    assert page.subtitle_label.get_layout().get_line_count() == 2
    ok, at = page.date_button.get_child().compute_point(page.subtitle_label, Graphene.Point().init(0, 0))
    assert ok and abs(at.x - page.subtitle_label.get_layout_offsets()[0]) <= 1

    # 8) A long date with Large Text (125%) wraps inside the button at the line's
    # own breaks: nothing is cut at the window's edge, the date's second line is
    # the button and the length after it on that line stays plain text.
    from cassini_talk.i18n import using_language

    with using_language("pt-BR"):
        long_date, long_rest = fmt.preview_subtitle_parts(
            1763024400000, 52 * 60_000, now_ms=window.now_ms, tz=window.tz
        )  # Thursday 13 November 2025, a year before "now": weekday, month and year spelled out
    Gtk.Settings.get_default().props.gtk_xft_dpi = int(96 * 1.25 * 1024)
    page.subtitle_line.set_parts(long_date, long_rest)
    button, inner, whole = page.date_button, page.date_button.get_child(), page.subtitle_label
    date_bytes = len(long_date.encode())

    def line_texts(widget):
        raw = widget.get_label().encode()
        return [
            raw[ln.start_index : ln.start_index + ln.length].decode() for ln in widget.get_layout().get_lines_readonly()
        ]

    def date_lines():
        return whole.get_layout().index_to_line_x(date_bytes - 1, False)[0] + 1

    def wrapped_laid_out():
        return (
            whole.get_label() == long_date + long_rest
            and date_lines() >= 2
            and button.wrapped
            and inner.get_layout().get_width() == whole.get_layout().get_width()
            and inner.get_layout().get_line_count() == date_lines()
        )

    until(wrapped_laid_out, "the long date did not wrap inside the button")
    print("Long date lines:", line_texts(inner), "line:", line_texts(whole), flush=True)
    ok, end = inner.compute_point(page.full_header, Graphene.Point().init(inner.get_width(), inner.get_height()))
    assert ok and end.x <= page.full_header.get_width(), "the long date runs past the narrow header"
    # The button's lines are the line's own: same breaks, same place.
    ours, theirs = line_texts(inner), line_texts(whole)
    assert "".join(ours) == long_date
    assert ours[:-1] == theirs[: len(ours) - 1] and theirs[len(ours) - 1].startswith(ours[-1]), (ours, theirs)
    ok, at = inner.compute_point(whole, Graphene.Point().init(0, 0))
    x, y = whole.get_layout_offsets()
    ix, iy = inner.get_layout_offsets()
    assert ok and abs(at.x + ix - x) <= 1 and abs(at.y + iy - y) <= 1, "the wrapped date is not over the line's"

    def label_point(index):
        pos = whole.get_layout().index_to_pos(index)
        return x + (pos.x + pos.width / 2) / Pango.SCALE, y + (pos.y + pos.height / 2) / Pango.SCALE

    last_date_char = long_date.encode().rindex(long_date[-1].encode())
    date_x, date_y = label_point(last_date_char)  # the time's last digit, on the date's last line
    rest_index = date_bytes + long_rest.encode().index(long_rest.strip()[-1].encode())
    plain_x, plain_y = label_point(rest_index)  # the length's last letter
    for px, py, expected in ((date_x, date_y, button), (plain_x, plain_y, whole)):
        ok, there = whole.compute_point(window, Graphene.Point().init(px, py))
        picked = window.pick(there.x, there.y, Gtk.PickFlags.DEFAULT)
        assert ok and picked is not None and (picked is expected or picked.is_ancestor(expected)), (
            px,
            py,
            picked,
        )
    if abs(plain_y - date_y) < 1:
        print("The length shares the date's last line", flush=True)
    click_plain(window, whole, plain_x, plain_y, "clicking the length after a wrapped date opened Set date and time")
    pointer(window, whole, x=date_x, y=date_y)
    until(lambda: button.get_state_flags() & Gtk.StateFlags.PRELIGHT, "the wrapped date's last line took no hover")
    pointer(window, whole, click=True, x=date_x, y=date_y)
    close_with_escape(window, wait_dialog(window, "Clicking the wrapped date's last line"))

    window._fixture_activity = window._activity = {}
    window.monitor.capture_activity = lambda: {}
    window.close()
    app.comfort.close()

print("PASS Date button: hover, click, Enter and Space open Set date and time; the rest stays plain text")
