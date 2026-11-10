#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact-container spike; run in a private QA GNOME desktop, synthetic only.

Register compiled src/cassini-talk.gresource in CASSINI_CUSTOM_BUILD, compile
schemas there, set GSETTINGS_SCHEMA_DIR there, and PYTHONPATH=src:tests.
Results use frame-clock observations and thread CPU time, never sleep polling.
"""

import faulthandler
import json
import os
import resource
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(Path(__file__).parent)]
os.environ["CASSINI_TRANSCRIPT_RENDER"] = "custom"
os.environ["CASSINI_SCREENSHOT_ISOLATED"] = "1"
os.environ["GSETTINGS_BACKEND"] = "memory"
Gio.resources_register(Gio.Resource.load(os.environ["CASSINI_CUSTOM_BUILD"] + "/cassini-talk.gresource"))
from transcript_measurements import check_measurements  # noqa: E402
from transcript_scroll_support import hour_meeting  # noqa: E402

from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_text import reading_turns, speaker_turns  # noqa: E402
from cassini_talk.open_meeting import load_open_meeting  # noqa: E402

faulthandler.dump_traceback_later(120, repeat=True)


def until(check):
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


class Frames:
    def __init__(self, window):
        self.clock = window.get_frame_clock()
        self.start = 0
        self.cpu = []
        self.intervals = []
        self.previous = None
        self.handlers = [self.clock.connect("before-paint", self.before), self.clock.connect("after-paint", self.after)]

    def before(self, *_):
        self.start = time.thread_time_ns()
        stamp = self.clock.get_frame_time()
        if self.previous is not None:
            self.intervals.append((stamp - self.previous) / 1000)
        self.previous = stamp

    def after(self, *_):
        self.cpu.append((time.thread_time_ns() - self.start) / 1e6)

    def close(self):
        for handler in self.handlers:
            self.clock.disconnect(handler)


def anchor(page):
    visible = []
    for index, (_, _, row) in page.bound.items():
        ok, bounds = row.compute_bounds(page.scroller)
        if ok and bounds.get_y() < page.scroller.get_height() and bounds.get_y() + bounds.get_height() > 0:
            expected = page.list.prefix[index + 1] - page.list.prefix[index]
            actual = row.measure(Gtk.Orientation.VERTICAL, page.list.layout_width).natural
            assert actual == expected, (index, actual, expected)
            visible.append((index, -bounds.get_y()))
    assert visible
    return min(visible)


def flicks(window, page, playing):
    adjustment = page.scroller.get_vadjustment()
    assert page.scroller.get_kinetic_scrolling()
    controllers = page.scroller.observe_controllers()
    native = [
        controllers.get_item(i)
        for i in range(controllers.get_n_items())
        if isinstance(controllers.get_item(i), Gtk.EventControllerScroll)
        and controllers.get_item(i).get_flags() & Gtk.EventControllerScrollFlags.KINETIC
        and not controllers.get_item(i).get_flags() & Gtk.EventControllerScrollFlags.DISCRETE
    ]
    assert len(native) == 2, native
    bubble = next(c for c in native if c.get_propagation_phase() == Gtk.PropagationPhase.BUBBLE)
    capture = next(c for c in native if c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE)
    page._follow_until = GLib.get_monotonic_time() + 3600_000_000
    if playing:
        page.player.play()
        until(lambda: page.player.playing)
    page.list.scroll_to(min(125, len(page.list.items) // 3))
    paint(window)
    paint(window)
    upper = adjustment.get_upper()
    samples, reverse, coast = [], 0, 0
    frames = Frames(window)
    for direction in (1, 1, 1, -1, -1, -1):
        previous = None

        def sample(direction=direction):
            nonlocal previous, reverse
            current = anchor(page)
            if previous and (
                direction * (current[0] - previous[0]) < 0
                or (current[0] == previous[0] and direction * (current[1] - previous[1]) < -1)
            ):
                reverse += 1
            previous = current
            samples.append((direction, current, adjustment.get_value(), adjustment.get_upper()))

        capture.emit("scroll-begin")
        bubble.emit("scroll-begin")
        for delta in (1.5, 2.0, 3.0, 4.0, 3.0):
            if not capture.emit("scroll", 0, direction * delta):
                bubble.emit("scroll", 0, direction * delta)
            paint(window)
            sample()
        released = adjustment.get_value()
        capture.emit("decelerate", 0, direction * 35.0)
        bubble.emit("scroll-end")
        unchanged = 0
        last = released
        for _ in range(240):
            paint(window)
            sample()
            value = adjustment.get_value()
            unchanged = unchanged + 1 if abs(value - last) < 1e-9 else 0
            last = value
            if unchanged >= 4:
                break
        assert unchanged >= 4, "Native coast did not settle within 240 frames"
        coast += abs(last - released)
    frames.close()
    if playing:
        page.player.pause()
    assert coast > 0, "Native kinetic coast did not run"
    assert reverse == 0, ("Visible anchor reversed", reverse)
    assert all(sample[3] == upper for sample in samples), "Scroll extent changed after load"
    return dict(
        reverseFrames=reverse,
        upperChanges=sum(s[3] != upper for s in samples),
        frames=len(samples),
        coastPixels=round(coast, 2),
        worstFrameCpuMs=max(frames.cpu),
        worstFrameIntervalMs=max(frames.intervals),
        samples=samples,
    )


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root / "EmptyLibrary")
    opus = hour_meeting(root / "Hour", mixed=True)
    only_plain = os.environ.get("CASSINI_CUSTOM_CASE") == "plain"
    long_opus = opus if only_plain else hour_meeting(root / "FourHours", mixed=True, hours=4)
    meeting = load_open_meeting(opus, root / "Hour")
    long_meeting = load_open_meeting(long_opus, root / "FourHours")
    plain_words = tuple(replace(word, speaker="person-0") for word in meeting.words)
    plain = replace(
        meeting,
        words=plain_words,
        turns=reading_turns(plain_words),
        listening_turns=speaker_turns(plain_words),
        speakers=(),
    )
    app = CassiniApplication("com.gocassini.CustomRenderSpike", "test", "development")
    app.transcription_enabled = False
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    window.monitor.callback = lambda *_: None
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player._pipeline.set_property("audio-sink", sink)
    paint(window)
    window.unmaximize()
    check_measurements(window)
    reports = []
    for name, model in (("60min-diarized", meeting), ("4h-diarized", long_meeting), ("60min-no-speakers", plain)):
        if only_plain and name != "60min-no-speakers":
            continue
        print("START", name, len(model.words), len(model.turns), flush=True)
        frames = Frames(window)
        started = time.thread_time_ns()
        window.open_meeting(model, external=True)
        page = window.meeting_page
        paint(window)
        until(lambda page=page: page.list.get_mapped() and page.list.ready and bool(page.bound))
        first = (time.thread_time_ns() - started) / 1e6
        until(lambda page=page: page.list.ready and bool(page.bound))
        paint(window)
        fully = (page.list.layout_completed_cpu_ns - started) / 1e6
        frames.close()
        report = dict(
            name=name,
            words=len(model.words),
            turns=len(model.turns),
            openFirstPaintCpuMs=first,
            openFullyLaidOutCpuMs=fully,
            openWorstFrameCpuMs=max(frames.cpu),
            upper=page.scroller.get_vadjustment().get_upper(),
        )
        until(lambda: window.player._prerolled)
        report["paused"] = flicks(window, page, False)
        report["playingFollowOff"] = flicks(window, page, True)
        costs = []
        for i in range(200):
            window.player.freeze_notify()
            window.player.position = round(i * model.duration_ms / 200)
            start = time.thread_time_ns()
            page._position()
            costs.append((time.thread_time_ns() - start) / 1e6)
            window.player.thaw_notify()
        report["highlightCpuMeanMs"] = sum(costs) / len(costs)
        report["highlightCpuMaxMs"] = max(costs)
        frames = Frames(window)
        widths = [page.list.layout_width]
        original_size = window.get_width(), window.get_height()
        window.set_default_size(original_size[0] + 80, original_size[1])
        until(lambda page=page, widths=widths: page.list.layout_width != widths[0])
        paint(window)
        widths.append(page.list.layout_width)
        window.set_default_size(*original_size)
        until(lambda page=page, widths=widths: page.list.layout_width == widths[0])
        paint(window)
        widths.append(page.list.layout_width)
        assert widths[0] != widths[1] and widths[0] == widths[2], widths
        report["resizeWidths"] = widths
        frames.close()
        report["resizeWorstFrameCpuMs"] = max(frames.cpu)
        report["rssMiB"] = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576
        report["peakRssMiB"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        report["boundRows"] = len(page.bound)
        # All host and folded-word URIs seek to the retained timed atom.
        from cassini_talk.meeting_text import reading_word

        for turn in model.turns:
            for i, word in enumerate(turn.words):
                assert reading_word(turn, f"item:{i}") is word
            for j, (_, aside) in enumerate(turn.asides):
                for i, word in enumerate(aside.words):
                    assert reading_word(turn, f"aside:{j}:{i}") is word
        # Exercise real GtkLabel activation and existing follow routing.
        target = model.turns[len(model.turns) // 2]
        page._follow_until = 0
        page.player.seek(target.start_ms)
        for _ in range(30):
            paint(window)
            if target.index in page.bound:
                break
        assert target.index in page.bound, (
            target.index,
            target.start_ms,
            page.player.position,
            page._reading_index,
            page._follow_rows.at(target.start_ms),
            sorted(page.bound),
            page._follow_until,
        )
        words = page.bound[target.index][1]
        assert 'background="#fbe7a1"' in words.get_label(), "Current word has no highlight"
        word = target.words[min(2, len(target.words) - 1)]
        words.emit("activate-link", f"item:{min(2, len(target.words) - 1)}")
        until(lambda page=page, word=word: page.player.position == word.start_ms)
        paint(window)
        assert page._reading_index == page._follow_rows.at(page.player.position)
        ok, bounds = page.bound[target.index][2].compute_bounds(page.scroller)
        assert ok and bounds.get_y() < page.scroller.get_height() and bounds.get_y() + bounds.get_height() > 0
        report["wordActivationAndFollow"] = True
        reports.append(report)
        Path(os.environ["CASSINI_CUSTOM_OUT"]).write_text(json.dumps(reports, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k not in ("paused", "playingFollowOff")}), flush=True)
        print(
            json.dumps(
                {
                    mode: {k: v for k, v in report[mode].items() if k != "samples"}
                    for mode in ("paused", "playingFollowOff")
                }
            ),
            flush=True,
        )
        window.back_to_library()
        paint(window)
    Path(os.environ["CASSINI_CUSTOM_OUT"]).write_text(json.dumps(reports, indent=2))
    app.on_quit()
    app.do_shutdown()
print("PASS custom transcript measurements", flush=True)
