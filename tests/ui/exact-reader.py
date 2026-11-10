#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone reader: deferred search, native scrolling and word activation."""

import os
import sys
import tempfile
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1":
    raise SystemExit("Use the private UI harness")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_CUSTOM_BUILD"]) / "cassini-talk.gresource")))

from transcript_scroll_support import hour_meeting  # noqa: E402

from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.window import CassiniWindow  # noqa: E402


def paint(window):
    loop = GLib.MainLoop()
    clock = window.get_frame_clock()
    handler = clock.connect("after-paint", lambda *_: loop.quit())
    window.add_tick_callback(lambda *_: GLib.SOURCE_REMOVE)
    try:
        loop.run()
    finally:
        clock.disconnect(handler)


def until(check):
    context = GLib.MainContext.default()
    while not check():
        context.iteration(True)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root / "library")
    file = Gio.File.new_for_path(str(hour_meeting(root / "Hour", mixed=True)))
    app = CassiniApplication("com.gocassini.ExactReaderTest", "test", "development")
    app.transcription_enabled = False
    app.register(None)
    window = CassiniWindow(application=app)
    window.set_default_size(1100, 820)
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player._pipeline.set_property("audio-sink", sink)
    window.present()
    window.open_file(file)
    view = window._exact_transcript
    assert view is not None, "The standalone test must exercise the default"
    until(lambda: bool(view.rows))
    assert not view.ready, "The first viewport should paint during indexing"

    # Search requests a turn that has not been measured yet.
    window.search_bar.set_search_mode(True)
    window.search_entry.set_text("instrumentation")
    until(lambda: len(window.matches) > 10 and window.match_cursor >= 0)
    assert not view.ready
    window.match_cursor = len(window.matches) - 2
    window._step_match(1)
    turn_index, word_index = window.matches[-1]
    assert window._word_scroll is not None
    until(lambda: view.ready and window._word_scroll is None and window.player._prerolled)
    paint(window)
    target = window.turns[turn_index]
    row = window._realized_turn(target.host.index if target.host is not None else turn_index)
    assert row and 'background="#e5a50a"' in row.words.get_label(), (
        turn_index,
        word_index,
        window.match_cursor,
        len(window.matches),
        list(view.rows),
    )
    ok, bounds = row.compute_bounds(window.transcript_scroller)
    assert ok and bounds.get_y() < window.transcript_scroller.get_height() and bounds.get_y() + bounds.get_height() > 0
    starts = []
    handler = window.player.connect(
        "notify::playing", lambda player, _p: starts.append(player.position) if player.playing else None
    )
    row.words.emit("activate-link", f"w:{turn_index}/{word_index}")
    until(lambda: starts)
    assert starts[0] == window.turns[turn_index].words[word_index].start_ms + 1
    window.player.pause()
    window.player.disconnect(handler)

    host = target.host.index if target.host is not None else turn_index
    action_key = window.visible_index[host], f"w:{turn_index}/{word_index}"
    until(lambda: action_key in view.word_actions.buttons)
    window.player.seek(0)
    view.word_actions.buttons[action_key].emit("clicked")
    until(lambda: window.player.playing)
    assert window.player.position >= target.words[word_index].start_ms + 1
    window.player.pause()

    # Actual native finger/coast handlers keep the exact extent and direction.
    adjustment = window.transcript_scroller.get_vadjustment()
    upper = adjustment.get_upper()
    adjustment.set_value(upper / 2)
    paint(window)
    model = window.transcript_scroller.observe_controllers()
    native = [
        controller
        for controller in model
        if isinstance(controller, Gtk.EventControllerScroll)
        and controller.get_flags() & Gtk.EventControllerScrollFlags.KINETIC
        and not controller.get_flags() & Gtk.EventControllerScrollFlags.DISCRETE
    ]
    capture = next(c for c in native if c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE)
    bubble = next(c for c in native if c.get_propagation_phase() == Gtk.PropagationPhase.BUBBLE)
    for direction in (1, -1):
        capture.emit("scroll-begin")
        bubble.emit("scroll-begin")
        initial = adjustment.get_value()
        for _ in range(5):
            if not capture.emit("scroll", 0, 3 * direction):
                bubble.emit("scroll", 0, 3 * direction)
            paint(window)
        released = adjustment.get_value()
        assert direction * (released - initial) > 0
        capture.emit("decelerate", 0, 30.0 * direction)
        bubble.emit("scroll-end")
        previous, steady = released, 0
        for _ in range(240):
            paint(window)
            value = adjustment.get_value()
            assert direction * (value - previous) >= -1
            assert adjustment.get_upper() == upper
            steady = steady + 1 if value == previous else 0
            previous = value
            if steady >= 4:
                break
        assert steady >= 4 and direction * (previous - released) > 0

    assert view.word_actions.buttons, "Visible reader words need native actions"
    old_rows = tuple(view.rows.values())
    window.open_file(file)
    assert not view.rows and all(row.get_parent() is None for row in old_rows)
    until(lambda: view.viewport_ready)
    window.close()
    app.do_shutdown()
print("PASS standalone exact reader: pending search, word seek, native coast, document reset")
