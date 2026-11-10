#!/usr/bin/python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fast kinetic gestures through real hour-long reader and Meeting widgets."""

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
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from transcript_scroll_support import hour_meeting  # noqa: E402

from cassini_talk.main import CassiniApplication  # noqa: E402
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


def check_surface(name, window, view, scroller, get_rows, motion):
    dispatch_until(lambda: scroller.get_mapped() and scroller.get_height() > 0)
    paint(window)
    view.scroll_to(125, Gtk.ListScrollFlags.NONE, None)
    paint(window)
    paint(window)
    adjustment = scroller.get_vadjustment()
    samples, cpu = [], []
    started = [0]
    clock = window.get_frame_clock()
    before = clock.connect("before-paint", lambda *_: started.__setitem__(0, time.thread_time_ns()))
    after = clock.connect("after-paint", lambda *_: cpu.append((time.thread_time_ns() - started[0]) / 1e6))
    raw = []
    changed = adjustment.connect(
        "value-changed", lambda *_: raw.append((adjustment.get_value(), adjustment.get_upper()))
    )
    peak_bound = [0]

    def snapshot(direction, previous):
        rows = get_rows()
        peak_bound[0] = max(peak_bound[0], len(rows))
        current = anchor(rows, scroller)
        samples.append((direction, current, adjustment.get_value(), adjustment.get_upper()))
        if previous is not None:
            assert direction * (current[0] - previous[0]) >= 0, (name, "row reversed", previous, current)
            if current[0] == previous[0]:
                assert direction * (current[1] - previous[1]) >= -1, (name, "anchor reversed", previous, current)
        return current

    try:
        for direction in (1, -1):
            previous = snapshot(direction, None)
            initial = previous
            controller = motion.controller
            controller.emit("scroll-begin")
            # Xvfb has no physical touchpad: drive the same real GTK scroll and
            # decelerate signals that receive its finger event/velocity history.
            for delta in (1, 2, 3, 2, 1):
                controller.emit("scroll", 0, direction * delta)
                paint(window)
                previous = snapshot(direction, previous)
            released = previous
            factor = motion._factor(controller)
            controller.emit("decelerate", 0, direction * 16000 / factor)
            assert motion.motion is not None
            while motion.motion is not None:
                paint(window)
                previous = snapshot(direction, previous)
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
        motion._key(None, Gdk.KEY_Page_Down, 0, Gdk.ModifierType(0))
        assert motion.motion is None and motion.tick == 0
        assert peak_bound[0] < 300, (name, "lost row virtualization", peak_bound[0])
    finally:
        motion.stop()
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
    Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".json").write_text(json.dumps(results))
    app.on_quit()
    app.do_shutdown()
print("PASS fast transcript gestures and stable anchors on both surfaces", flush=True)
