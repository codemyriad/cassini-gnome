#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact transcript UI regression and CPU budgets; isolated desktop, synthetic only.

Register compiled src/cassini-talk.gresource in CASSINI_CUSTOM_BUILD, compile
schemas there, set GSETTINGS_SCHEMA_DIR there, and PYTHONPATH=src:tests.
Results use frame-clock observations and thread CPU time, never sleep polling.
"""

import faulthandler
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1":
    raise SystemExit("Run in the private UI harness")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(Path(__file__).parent)]
BASELINE = os.environ.get("CASSINI_TRANSCRIPT_RENDER") == "listview"
os.environ["CASSINI_SCREENSHOT_ISOLATED"] = "1"
os.environ["GSETTINGS_BACKEND"] = "memory"
Gio.resources_register(
    Gio.Resource.load(
        os.environ.get("CASSINI_CUSTOM_BUILD", os.environ.get("CASSINI_TEST_BUILD", "") + "/src")
        + "/cassini-talk.gresource"
    )
)
from exact_budgets import (  # noqa: E402
    FIRST_PAINT_CEILING_MS,
    FIRST_PAINT_TARGET_MS,
    FRAME_CPU_CEILING_MS,
    FRAME_CPU_TARGET_MS,
    HIGHLIGHT_CPU_CEILING_MS,
    HIGHLIGHT_CPU_TARGET_MS,
    check_cpu_budget,
)
from exact_interactions import native_scroll  # noqa: E402
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
        self.previous_cpu = time.thread_time_ns()
        self.between_paints_cpu = []
        self.handlers = [self.clock.connect("before-paint", self.before), self.clock.connect("after-paint", self.after)]

    def before(self, *_):
        self.start = time.thread_time_ns()
        stamp = self.clock.get_frame_time()
        if self.previous is not None:
            self.intervals.append((stamp - self.previous) / 1000)
        self.previous = stamp

    def after(self, *_):
        now = time.thread_time_ns()
        self.cpu.append((now - self.start) / 1e6)
        self.between_paints_cpu.append((now - self.previous_cpu) / 1e6)
        self.previous_cpu = now

    def close(self):
        for handler in self.handlers:
            self.clock.disconnect(handler)


def anchor(page):
    visible = []
    for index, (_, _, row) in page.bound.items():
        ok, bounds = row.compute_bounds(page.scroller)
        if ok and bounds.get_y() < page.scroller.get_height() and bounds.get_y() + bounds.get_height() > 0:
            if not BASELINE:
                expected = page.list.prefix[index + 1] - page.list.prefix[index]
                actual = row.measure(Gtk.Orientation.VERTICAL, page.list.layout_width).natural
                assert actual == expected, (index, actual, expected)
            visible.append((index, -bounds.get_y()))
    assert visible
    return min(visible)


def flicks(window, page, playing, touch=False, label="scroll"):
    adjustment = page.scroller.get_vadjustment()
    assert page.scroller.get_kinetic_scrolling()
    controllers, bubble, capture = native_scroll(page)
    page._follow_until = GLib.get_monotonic_time() + 3600_000_000
    if playing:
        page.player.play()
        until(lambda: page.player.playing)
    page.list.scroll_to(min(125, len(page.meeting.turns) // 3), Gtk.ListScrollFlags.NONE, None)
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

        if touch:
            gestures = [controllers.get_item(i) for i in range(controllers.get_n_items())]
            drag = next(c for c in gestures if type(c) is Gtk.GestureDrag)
            swipe = next(c for c in gestures if type(c) is Gtk.GestureSwipe)
            drag.emit("drag-begin", 0, 0)
            offset = 0
        else:
            capture.emit("scroll-begin")
            bubble.emit("scroll-begin")
        for delta in (1.5, 2.0, 3.0, 4.0, 3.0):
            if touch:
                offset -= direction * delta * 70
                drag.emit("drag-update", 0, offset)
            elif not capture.emit("scroll", 0, direction * delta):
                bubble.emit("scroll", 0, direction * delta)
            paint(window)
            sample()
        released = adjustment.get_value()
        if touch:
            swipe.emit("swipe", 0, -direction * 2100.0)
        else:
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
    check_cpu_budget(
        label,
        "scrollWorstFrameCpuMs",
        max(frames.cpu),
        target_ms=FRAME_CPU_TARGET_MS,
        ceiling_ms=FRAME_CPU_CEILING_MS,
    )
    if not BASELINE:
        assert reverse == 0, ("Visible anchor reversed", reverse)
    if not BASELINE:
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


def memory_check():
    memory = subprocess.check_output(["free", "-m"], text=True)
    print(memory, flush=True)
    available = int(next(line for line in memory.splitlines() if line.startswith("Mem:")).split()[-1])
    assert available > 4000, ("RAM rule: insufficient available MB", available)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root / "EmptyLibrary")
    opus = hour_meeting(root / "Hour", mixed=True)
    only_plain = os.environ.get("CASSINI_CUSTOM_CASE") == "plain"
    only_hour = os.environ.get("CASSINI_CUSTOM_CASE") == "hour"
    only_long = os.environ.get("CASSINI_CUSTOM_CASE") == "long"
    memory_check()
    long_opus = opus if only_plain or only_hour else hour_meeting(root / "FourHours", mixed=True, hours=4)
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
    app = CassiniApplication("com.gocassini.ExactTranscriptTest", "test", "development")
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
        if only_hour and name != "60min-diarized":
            continue
        if only_plain and name != "60min-no-speakers":
            continue
        if only_long and name != "4h-diarized":
            continue
        print("START", name, len(model.words), len(model.turns), flush=True)
        if name == "4h-diarized":
            memory_check()
        if os.environ.get("CASSINI_EXACT_INTERACTIONS_ONLY"):
            window.open_meeting(model, external=True)
            page = window.meeting_page
            until(lambda page=page: page.list.ready and window.player._prerolled)
            paint(window)
            from exact_interactions import check_interactions

            print(json.dumps(check_interactions(window, page, paint, until)), flush=True)
            window.back_to_library()
            continue
        frames = Frames(window)
        started = time.thread_time_ns()
        frame_started = window.get_frame_clock().get_frame_time()
        window.open_meeting(model, external=True)
        opened_cpu = time.thread_time_ns()
        page = window.meeting_page
        assert page._exact is not BASELINE
        paint(window)
        if BASELINE:
            until(lambda page=page: page.list.get_mapped() and bool(page.bound))
            for _ in range(8):
                paint(window)
            assert all(not words.get_selectable() for _, words, _ in page.bound.values())
            assert not hasattr(page, "search_entry")
            if name == "60min-diarized":
                from exact_interactions import accessible

                print("BASELINE ATSPI", json.dumps(accessible(window, page, paint, until, baseline=True)), flush=True)
            rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576
            reports.append(dict(name=name, rssMiB=rss, boundRows=len(page.bound)))
            Path(os.environ["CASSINI_CUSTOM_OUT"]).write_text(json.dumps(reports, indent=2))
            print(json.dumps(reports[-1]), flush=True)
            window.back_to_library()
            paint(window)
            continue
        until(lambda page=page: page.list.get_mapped() and page.list.viewport_ready and bool(page.bound))
        first = (time.thread_time_ns() - started) / 1e6
        first_frame_ms = (window.get_frame_clock().get_frame_time() - frame_started) / 1000
        first_rows = len(page.list.prefix) - 1
        load_samples = []
        _, bubble, capture = native_scroll(page)
        page._follow_until = GLib.get_monotonic_time() + 3600_000_000
        previous = anchor(page)
        load_reverse = 0
        capture.emit("scroll-begin")
        bubble.emit("scroll-begin")
        while not page.list.ready:
            if len(load_samples) >= 5 and not capture.emit("scroll", 0, 0.4):
                bubble.emit("scroll", 0, 0.4)
            paint(window)
            current = anchor(page)
            if current[0] < previous[0] or (current[0] == previous[0] and current[1] < previous[1] - 1):
                load_reverse += 1
            load_samples.append((current, page.scroller.get_vadjustment().get_upper()))
            previous = current
        bubble.emit("scroll-end")
        paint(window)
        assert load_reverse == 0, ("Indexing scroll anchor reversed", load_reverse)
        assert all(
            sample[0][0] == 0 and abs(sample[0][1] - load_samples[0][0][1]) <= 1 for sample in load_samples[:5]
        ), load_samples[:5]
        fully_frame_ms = (window.get_frame_clock().get_frame_time() - frame_started) / 1000
        fully = (page.list.layout_completed_cpu_ns - started) / 1e6
        frames.close()
        report = dict(
            name=name,
            words=len(model.words),
            turns=len(model.turns),
            openFirstPaintCpuMs=first,
            openPageConstructionCpuMs=(opened_cpu - started) / 1e6,
            openFirstPaintFrameClockMs=first_frame_ms,
            rowsMeasuredAtFirstPaint=first_rows,
            openFullyLaidOutFrameClockMs=fully_frame_ms,
            openBuildSliceMaxCpuMs=page.list.build_slice_cpu_max_ns / 1e6,
            openMeasurementCpuMs=page.list.measurement_cpu_ns / 1e6,
            loadAnchorChanges=0,
            loadReverseFrames=load_reverse,
            loadUpperChanges=sum(b[1] != a[1] for a, b in zip(load_samples, load_samples[1:], strict=False)),
            openWorstBetweenPaintsCpuMs=max(frames.between_paints_cpu),
            openFullyLaidOutCpuMs=fully,
            openWorstFrameCpuMs=max(frames.cpu),
            upper=page.scroller.get_vadjustment().get_upper(),
        )
        print("OPEN_PROFILE", json.dumps(report), flush=True)
        until(lambda: window.player._prerolled)
        report["paused"] = flicks(window, page, False, label=f"{name}.paused")
        report["playingFollowOff"] = flicks(window, page, True, label=f"{name}.playingFollowOff")
        report["touchPaused"] = flicks(window, page, False, touch=True, label=f"{name}.touchPaused")
        report["touchPlaying"] = flicks(window, page, True, touch=True, label=f"{name}.touchPlaying")
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
        check_cpu_budget(
            name,
            "highlightCpuMaxMs",
            max(costs),
            target_ms=HIGHLIGHT_CPU_TARGET_MS,
            ceiling_ms=HIGHLIGHT_CPU_CEILING_MS,
        )
        # A retained overscan row skips playback work until it enters the
        # viewport. Its first painted frame must already highlight the seek.
        outside = next(i for i in sorted(page.bound) if i >= page.list.visible_rows.stop)
        turn, overscan_words, _ = page.bound[outside]
        word = next(w for w, value in zip(turn.words, turn.items, strict=True) if value and w.end_ms > w.start_ms)
        page._follow_until = GLib.get_monotonic_time() + 3600_000_000
        page.player.seek(word.start_ms)
        paint(window)
        assert outside not in page.list.visible_rows and 'background="#fbe7a1"' not in overscan_words.get_label()
        page.list.scroll_to(outside)
        paint(window)
        assert page.bound[outside][1] is overscan_words
        assert 'background="#fbe7a1"' in overscan_words.get_label()
        report["overscanHighlightOnEntry"] = True
        # Above the cap: real resize, same layout width and zero measurement.
        frames = Frames(window)
        widths = [page.list.layout_width]
        original_size = window.get_default_size()
        original_width = window.get_width()
        measured = page.list.measured_rows
        window.set_default_size(original_size[0] + 80, original_size[1])
        until(lambda original_width=original_width: window.get_width() != original_width)
        paint(window)
        widths.append(page.list.layout_width)
        window.set_default_size(*original_size)
        until(lambda original_width=original_width: window.get_width() == original_width)
        paint(window)
        widths.append(page.list.layout_width)
        frames.close()
        assert widths[0] == widths[1] == widths[2], widths
        assert page.list.measured_rows == measured
        report["resizeAboveCapWidths"] = widths
        report["resizeAboveCapWorstFrameCpuMs"] = max(frames.cpu)
        # Uncached narrow width, then return to the previous cached width.
        frames = Frames(window)
        window.set_default_size(860, original_size[1])
        until(lambda page=page, widths=widths: page.list._requested_width < widths[0])
        # Cancel an in-flight rebuild, return to the still-active cached
        # width, then resume the partial narrow index without repeating rows.
        until(lambda page=page, model=model: page.list.measured_rows > len(model.turns))
        partial_rows = page.list.measured_rows
        window.set_default_size(*original_size)
        until(lambda page=page, widths=widths: page.list._requested_width == widths[0] and page.list.ready)
        paint(window)
        cancelled_rows = page.list.measured_rows
        paint(window)
        assert page.list.measured_rows == cancelled_rows
        window.set_default_size(860, original_size[1])
        until(lambda page=page, widths=widths: page.list._requested_width < widths[0])
        until(lambda page=page: page.list.ready)
        paint(window)
        narrow = page.list.layout_width
        assert page.list.measured_rows == len(model.turns) * 2, "A cached turn/width was remeasured"
        report["resizeCancelledRowsReused"] = partial_rows - len(model.turns)
        measured = page.list.measured_rows
        window.set_default_size(*original_size)
        until(lambda page=page, widths=widths: page.list.layout_width == widths[0])
        paint(window)
        assert page.list.measured_rows == measured, "Returning to cached width remeasured rows"
        frames.close()
        report["resizeWidths"] = [widths[0], narrow, page.list.layout_width]
        report["resizeWorstFrameCpuMs"] = max(frames.cpu)
        report["resizeWorstBetweenPaintsCpuMs"] = max(frames.between_paints_cpu)
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
        assert report["openFirstPaintCpuMs"] < report["openFullyLaidOutCpuMs"], report
        check_cpu_budget(
            name,
            "openFirstPaintCpuMs",
            report["openFirstPaintCpuMs"],
            target_ms=FIRST_PAINT_TARGET_MS,
            ceiling_ms=FIRST_PAINT_CEILING_MS,
        )
        check_cpu_budget(
            name,
            "openWorstFrameCpuMs",
            report["openWorstFrameCpuMs"],
            target_ms=FRAME_CPU_TARGET_MS,
            ceiling_ms=FRAME_CPU_CEILING_MS,
        )
        check_cpu_budget(
            name,
            "resizeWorstFrameCpuMs",
            report["resizeWorstFrameCpuMs"],
            target_ms=FRAME_CPU_TARGET_MS,
            ceiling_ms=FRAME_CPU_CEILING_MS,
        )
        check_cpu_budget(
            name,
            "resizeAboveCapWorstFrameCpuMs",
            report["resizeAboveCapWorstFrameCpuMs"],
            target_ms=FRAME_CPU_TARGET_MS,
            ceiling_ms=FRAME_CPU_CEILING_MS,
        )
        if name == "60min-diarized":
            from exact_interactions import check_interactions

            report["interactions"] = check_interactions(window, page, paint, until)
        if name == "4h-diarized":
            from exact_interactions import native_scroll
            from exact_rebuild import check_presentation, check_rebuild

            page.player.pause()
            page._follow_until = GLib.get_monotonic_time() + 3600_000_000
            report["deepRebuildFrames"] = check_rebuild(window, page, paint, until, native_scroll)
            report["fontChangeScroll"] = {}
            report["deepPresentationFrames"] = check_presentation(
                window, page, model, paint, until, native_scroll, report["fontChangeScroll"]
            )
            window.set_default_size(*original_size)
            until(lambda page=page, widths=widths: page.list.ready and page.list.layout_width == widths[0])
            paint(window)
        reports.append(report)
        Path(os.environ["CASSINI_CUSTOM_OUT"]).write_text(json.dumps(reports, indent=2))
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k not in ("paused", "playingFollowOff", "touchPaused", "touchPlaying")
                }
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    mode: {k: v for k, v in report[mode].items() if k != "samples"}
                    for mode in ("paused", "playingFollowOff", "touchPaused", "touchPlaying")
                }
            ),
            flush=True,
        )
        window.back_to_library()
        paint(window)
    if not BASELINE and len(reports) == 3:
        assert reports[1]["openMeasurementCpuMs"] < 8 * reports[0]["openMeasurementCpuMs"], "Superlinear indexing"
        from exact_long_turn import check_long_turn

        reports[1]["longRejoinedTurn"] = check_long_turn(window, long_meeting, Frames, paint, until, flicks)
        print(
            "LONG_REJOINED_TURN",
            json.dumps(
                {k: v for k, v in reports[1]["longRejoinedTurn"].items() if k not in ("paused", "playingFollowOff")}
            ),
            flush=True,
        )
    Path(os.environ["CASSINI_CUSTOM_OUT"]).write_text(json.dumps(reports, indent=2))
    app.on_quit()
    app.do_shutdown()
print("PASS custom transcript measurements", flush=True)
