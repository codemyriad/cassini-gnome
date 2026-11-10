#!/usr/bin/python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fast kinetic gestures through real hour-long reader and Meeting widgets."""

import faulthandler
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
faulthandler.dump_traceback_later(60, repeat=True)
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import GdkX11, Gio, GLib, Graphene, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from listening_input import key  # noqa: E402
from transcript_measurements import check_measurements  # noqa: E402
from transcript_scroll_support import hour_meeting  # noqa: E402

from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.open_meeting import load_open_meeting  # noqa: E402
from cassini_talk.window import CassiniWindow  # noqa: E402


def dispatch_until(check):
    context = GLib.MainContext.default()
    while not check():
        context.iteration(True)


def paint(window):
    loop = GLib.MainLoop()
    clock = window.get_frame_clock()
    handler = clock.connect("after-paint", lambda *_: loop.quit())
    window.add_tick_callback(lambda *_: GLib.SOURCE_REMOVE)
    try:
        loop.run()
    finally:
        clock.disconnect(handler)


def anchor(rows, scroller):
    visible = []
    for index, row in rows.items():
        parent = row.get_parent()
        if not row.get_mapped() or not parent.get_child_visible():
            continue
        ok, bounds = parent.compute_bounds(scroller)
        if ok and bounds.get_y() < scroller.get_height() and bounds.get_y() + bounds.get_height() > 0:
            visible.append((index, max(0, -round(bounds.get_y())), round(bounds.get_height())))
    assert visible, "No transcript row is visible"
    return min(visible)


def start_motion(motion):
    motion.controller.emit("decelerate", 0, 16000 / motion._factor(motion.controller))
    assert motion.motion is not None


def real_key(window, sequence):
    assert window.get_focus() is not None, "Key input needs a focused transcript widget"
    released = []
    controller = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    controller.connect("key-released", lambda *_: released.append(True))
    window.add_controller(controller)
    try:

        def ready(_window, check):
            assert check(), "Native key target must be mapped before starting momentum"

        key(window, sequence, ready)
        dispatch_until(lambda: released)
    finally:
        window.remove_controller(controller)


def pointer_drag(window, widget, x, y, dy, dx=0):
    point = Graphene.Point()
    point.init(x, y)
    ok, target = widget.compute_point(window, point)
    assert ok
    released = []
    events = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    for signal in ("drag-end", "cancel"):
        events.connect(signal, lambda *_: released.append(True))
    window.add_controller(events)
    surface_x, surface_y = window.get_surface_transform()
    xid = str(GdkX11.X11Surface.get_xid(window.get_surface()))
    subprocess.run(
        [
            "xdotool",
            "windowfocus",
            "--sync",
            xid,
            "mousemove",
            "--window",
            xid,
            str(round(target.x + surface_x)),
            str(round(target.y + surface_y)),
            "mousedown",
            "1",
            "mousemove",
            "--window",
            xid,
            str(round(target.x + surface_x + dx)),
            str(round(target.y + surface_y + dy)),
            "mouseup",
            "1",
        ],
        check=True,
        timeout=5,
    )
    try:
        dispatch_until(lambda: released)
    finally:
        window.remove_controller(events)


def check_surface(name, window, view, scroller, get_rows, motion):
    dispatch_until(lambda: scroller.get_mapped() and scroller.get_height() > 0)
    paint(window)
    view.scroll_to(125, Gtk.ListScrollFlags.NONE, None)
    paint(window)
    paint(window)
    adjustment = scroller.get_vadjustment()
    samples, cpu, manual = [], [], []
    original_manual = motion.manual_scroll

    def user_input():
        manual.append(True)
        original_manual()

    motion.manual_scroll = user_input
    started = [0]
    clock = window.get_frame_clock()
    before = clock.connect("before-paint", lambda *_: started.__setitem__(0, time.thread_time_ns()))
    after = clock.connect("after-paint", lambda *_: cpu.append((time.thread_time_ns() - started[0]) / 1e6))
    raw = []
    changed = adjustment.connect(
        "value-changed", lambda *_: raw.append((adjustment.get_value(), adjustment.get_upper()))
    )
    peak_bound = [0]
    phase = "finger"

    def snapshot(direction, previous):
        rows = get_rows()
        peak_bound[0] = max(peak_bound[0], len(rows))
        current = anchor(rows, scroller)
        samples.append((direction, phase, current, adjustment.get_value(), adjustment.get_upper()))
        if previous is not None:
            assert direction * (current[0] - previous[0]) >= 0, (name, phase, "row reversed", previous, current)
            if current[0] == previous[0]:
                assert direction * (current[1] - previous[1]) >= -1, (name, phase, "anchor reversed", previous, current)
        return current

    try:
        assert not scroller.get_kinetic_scrolling(), "GTK absolute touch kinetic ticker is still enabled"
        for touch, direction in ((False, 1), (False, -1), (True, 1), (True, -1)):
            phase = "finger"
            previous = snapshot(direction, None)
            initial = previous
            controller = motion.controller
            if touch:
                motion.touch_drag.emit("drag-begin", 0, 0)
            else:
                controller.emit("scroll-begin")
            # Xvfb has no physical touchpad: drive the same real GTK scroll and
            # decelerate signals that receive its finger event/velocity history.
            displacement = 0
            for pixels in (75, 138, 225, 213, 163):
                if touch:
                    displacement -= direction * pixels
                    motion.touch_drag.emit("drag-update", 0, displacement)
                else:
                    controller.emit("scroll", 0, direction * pixels / motion._factor(controller))
                paint(window)
                previous = snapshot(direction, previous)
            if not touch:
                # A slow render can deliver several finger events before the
                # next allocation. Cover the entire batch, not just inertia.
                for pixels in (20, 25, 30):
                    controller.emit("scroll", 0, direction * pixels / motion._factor(controller))
                paint(window)
                previous = snapshot(direction, previous)
            released = previous
            phase = "coast"
            factor = motion._factor(controller)
            if touch:
                motion.touch_swipe.emit("swipe", 0, -direction * 16000)
            else:
                controller.emit("decelerate", 0, direction * 16000 / factor)
            assert motion.motion is not None
            notifications = len(manual)
            while motion.motion is not None:
                paint(window)
                previous = snapshot(direction, previous)
                assert len(manual) == notifications, "Inertia was reported as fresh user input"
            assert direction * (previous[0] - initial[0]) >= 2, (name, "gesture did not move content")
            assert direction * (previous[0] - released[0]) >= 2, (name, "kinetic motion was lost")
            stopped = previous
            # A new allocation without input must preserve the anchored row.
            for _ in range(3):
                paint(window)
                current = anchor(get_rows(), scroller)
                assert current[0] == stopped[0] and abs(current[1] - stopped[1]) <= 1, (
                    name,
                    "idle anchor moved",
                    stopped,
                    current,
                )
        # A reverse input owns the view immediately, cancelling old momentum.
        motion.controller.emit("decelerate", 0, 16000 / motion._factor(motion.controller))
        assert motion.motion is not None
        motion.controller.emit("scroll", 0, -1)
        assert motion.motion is None and motion.tick == 0
        motion.controller.emit("decelerate", 0, 16000 / motion._factor(motion.controller))
        assert motion.motion is not None
        for sequence in ("Page_Down", "KP_Page_Down"):
            scroller.set_focusable(True)
            assert scroller.grab_focus()
            paint(window)
            start_motion(motion)
            real_key(window, sequence)
            assert motion.motion is None and motion.tick == 0, (name, sequence)
        # Alt navigation belongs to listening, not manual transcript scrolling.
        scroller.set_focusable(True)
        assert scroller.grab_focus()
        paint(window)
        start_motion(motion)
        original = motion.motion
        real_key(window, "alt+Down")
        assert motion.motion is original, (name, "Alt+Down was treated as a manual scroll")
        motion.stop()
        paint(window)
        scrollbar = scroller.get_vscrollbar()
        start_motion(motion)
        pointer_drag(window, scrollbar, scrollbar.get_width() / 2, scrollbar.get_height() / 2, 25)
        assert motion.motion is None and motion.tick == 0, (name, "scrollbar did not take over")
        assert peak_bound[0] < 300, (name, "lost row virtualization", peak_bound[0])
    finally:
        motion.stop()
        motion.manual_scroll = original_manual
        adjustment.disconnect(changed)
        clock.disconnect(before)
        clock.disconnect(after)
    report = dict(
        surface=name,
        frames=len(cpu),
        peakBoundRows=peak_bound[0],
        frameCpuMaxMs=max(cpu),
        frameCpuP90Ms=sorted(cpu)[int(len(cpu) * 0.9)],
        rawCoordinateRebases=sum(b[0] < a[0] for a, b in zip(raw, raw[1:], strict=False)),
    )
    print(json.dumps(report), flush=True)
    return dict(report=report, samples=samples, raw=raw)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    opus = hour_meeting(root / "Library")
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(opus.parent))
    app = CassiniApplication("com.gocassini.TranscriptScrollTest", "test", "development")
    app.transcription_enabled = False
    app.register(None)
    app.activate()
    library = app.get_active_window()
    dispatch_until(lambda: not library._pending)
    library.monitor.callback = lambda *_: None
    reader = CassiniWindow(application=app)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    reader.player._pipeline.set_property("audio-sink", sink)
    reader.open_file(Gio.File.new_for_path(str(opus)))
    reader.present()
    check_measurements(reader)
    dispatch_until(lambda: len(reader.turns) == 480)
    results = [
        check_surface(
            "reader",
            reader,
            reader.transcript_list,
            reader.transcript_scroller,
            lambda: reader.rows,
            reader._transcript_scroll,
        )
    ]
    motion = reader._transcript_scroll
    start_motion(motion)
    reader.search_bar.set_search_mode(True)
    reader.search_entry.set_text("internationalization")
    dispatch_until(lambda: reader.matches)
    reader._step_match(1)
    print("search navigation requested", flush=True)
    assert motion.motion is None, "Search jump left momentum active"
    target = reader.matches[reader.match_cursor][0]
    dispatch_until(lambda: target in reader.rows and reader.rows[target].get_mapped())
    paint(reader)
    assert anchor(reader.rows, reader.transcript_scroller)[0] <= target
    reader.settings.set_boolean("follow-along", False)
    start_motion(motion)
    reader.settings.set_boolean("follow-along", True)
    assert motion.motion is None and reader.settings.get_boolean("follow-along")
    start_motion(motion)
    reader.position_scale.emit("change-value", Gtk.ScrollType.JUMP, 240000)
    assert motion.motion is None
    for action in ("skip-back", "skip-forward", "nudge-back", "nudge-forward"):
        start_motion(motion)
        reader.lookup_action(action).activate(None)
        assert motion.motion is None, action
    start_motion(motion)
    reader.seek_and_play(120000)
    assert motion.motion is None
    dispatch_until(lambda: reader.player.playing)
    reader.player.pause()
    reader.settings.set_boolean("follow-along", True)
    start_motion(motion)
    reader.toggle_playback()
    assert motion.motion is None and reader.settings.get_boolean("follow-along")
    paint(reader)
    assert reader.settings.get_boolean("follow-along"), "Coasting tick undid Play/Follow Along"
    reader.close()
    meeting = next(m for m in library.snapshot.meetings if m.published_file == opus)
    library.open_meeting(meeting)
    library.present()
    page = library.meeting_page
    results.append(
        check_surface(
            "meeting",
            library,
            page.list,
            page.scroller,
            lambda: {index: row for index, (_turn, _words, row) in page.bound.items()},
            page._transcript_scroll,
        )
    )
    start_motion(page._transcript_scroll)
    pointer_drag(library, page.seek, page.seek.get_width() * 0.3, page.seek.get_height() / 2, 0, dx=120)
    dispatch_until(lambda: page.timeline._dragged and not page.timeline._dragging)
    assert page._transcript_scroll.motion is None and page._follow_until == 0
    paint(library)
    target = page._follow_rows.at(page.player.position)
    assert page._reading_index == target, "Timeline seek did not select the sounding row"
    dispatch_until(lambda: target in page.bound and page.bound[target][2].get_mapped())
    assert page.bound[target][1].grab_focus()
    paint(library)
    start_motion(page._transcript_scroll)
    real_key(library, "Return")
    assert page._transcript_scroll.motion is None and page._follow_until == 0, "Word link left momentum active"
    for action in ("skip-back", "skip-forward"):
        start_motion(page._transcript_scroll)
        library.lookup_action(action).activate(None)
        assert page._transcript_scroll.motion is None and page._follow_until == 0, action
    # The reported Library case has about 490 turns, two speakers, many
    # short rows, long paragraphs, overlap captions and inline interjections.
    mixed_opus = hour_meeting(root / "MixedLibrary", mixed=True)
    mixed = load_open_meeting(mixed_opus, root / "MixedLibrary")
    assert len(mixed.speakers) == 2 and 450 <= len(mixed.turns) <= 550
    assert any(turn.over for turn in mixed.turns) and any(turn.asides for turn in mixed.turns)
    library.back_to_library()
    library.open_meeting(mixed, external=True)
    page = library.meeting_page
    results.append(
        check_surface(
            "meeting-diarized",
            library,
            page.list,
            page.scroller,
            lambda: {index: row for index, (_turn, _words, row) in page.bound.items()},
            page._transcript_scroll,
        )
    )
    # Exercise the plain paragraph geometry too: no sidebar/header column.
    # This varies presentation only; the word atoms remain the same fixture.
    page.split.set_collapsed(True)
    page.split.set_show_content(True)
    # The factory already owns the bound callback; hide new headers at map
    # time as well as existing rows, without rebuilding transcript words.
    factory = page.list.get_factory()
    handler = factory.connect_after("bind", lambda _f, item: item.get_child().get_first_child().set_visible(False))
    for _turn, _words, row in page.bound.values():
        row.get_first_child().set_visible(False)
    results.append(
        check_surface(
            "meeting-plain-geometry",
            library,
            page.list,
            page.scroller,
            lambda: {index: row for index, (_turn, _words, row) in page.bound.items()},
            page._transcript_scroll,
        )
    )
    factory.disconnect(handler)
    mixed_reader = CassiniWindow(application=app)
    mixed_reader.open_file(Gio.File.new_for_path(str(mixed_opus)))
    mixed_reader.present()
    dispatch_until(lambda: len(mixed_reader.turns) >= 480)
    results.append(
        check_surface(
            "reader-diarized",
            mixed_reader,
            mixed_reader.transcript_list,
            mixed_reader.transcript_scroller,
            lambda: mixed_reader.rows,
            mixed_reader._transcript_scroll,
        )
    )
    mixed_reader.close()
    Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".json").write_text(json.dumps(results))
    app.on_quit()
    app.do_shutdown()
print("PASS fast transcript gestures and stable anchors on both surfaces", flush=True)
