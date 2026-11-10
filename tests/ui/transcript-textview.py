#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-buffer spike measurements on a private headless GNOME desktop.

Run with PYTHONPATH=src:tests CASSINI_TRANSCRIPT_RENDER=textview,
CASSINI_TEXTVIEW_RESOURCE, CASSINI_TEXTVIEW_OUTPUT and an isolated Library.
Synthetic fixtures and frame-clock/CPU methodology originate in PR #310.
"""

import faulthandler
import json
import os
import shutil
import sys
import tempfile
import time
from bisect import bisect_left
from dataclasses import replace
from pathlib import Path

faulthandler.dump_traceback_later(120, repeat=True)
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gio, Graphene, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(os.environ["CASSINI_TEXTVIEW_RESOURCE"]))
from transcript_measurements import FrameCPU, dispatch_until, layout_idle, paint  # noqa: E402
from transcript_scroll_support import hour_meeting  # noqa: E402

from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_text import reading_turns, speaker_turns  # noqa: E402
from cassini_talk.open_meeting import load_open_meeting  # noqa: E402


def rss():
    return (
        int(
            next(
                line.split()[1]
                for line in Path("/proc/self/status").read_text().splitlines()
                if line.startswith("VmRSS:")
            )
        )
        / 1024
    )


def anchor(view):
    rect = view.get_visible_rect()
    _found, iterator = view.get_iter_at_location(rect.x + 225, rect.y + 1)
    location = view.get_iter_location(iterator)
    return iterator.get_offset(), rect.y - location.y


def check_controls(window, page):
    view = page.textview
    # The scroll cases deliberately disable following while native inertia
    # runs. End that gesture before enabling follow (normal UI suppression
    # lasts four seconds); otherwise the two intentionally compete.
    controllers = page.scroller.observe_controllers()
    for i in range(controllers.get_n_items()):
        controller = controllers.get_item(i)
        if (
            isinstance(controller, Gtk.EventControllerScroll)
            and controller.get_flags() & Gtk.EventControllerScrollFlags.KINETIC
            and controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        ):
            controller.emit("scroll-begin")
    page._follow_until = 0
    page._reading_index = -1
    target = view.ranges[len(view.ranges) // 2][2].start_ms
    page.player.seek(target)
    paint(window)
    layout_idle(window)
    # scroll_to_iter may animate its GtkAdjustment after the layout idles.
    # Wait for a stable allocation/scroll position before synthesizing input.
    adjustment = page.scroller.get_vadjustment()
    previous, stable = adjustment.get_value(), 0
    for _ in range(120):
        paint(window)
        current = adjustment.get_value()
        stable = stable + 1 if abs(current - previous) < 0.1 else 0
        previous = current
        if stable >= 3:
            break
    assert stable >= 3, "Follow scroll did not settle"
    index = page._follow_rows.at(target)
    assert index >= 0 and page._reading_index == index
    assert view._active, "Seek target has no active word"
    location = view.get_iter_location(view.buffer.get_iter_at_offset(view.ranges[min(view._active)][0]))
    visible = view.get_visible_rect()
    assert visible.y <= location.y < visible.y + visible.height, (
        "follow target invisible",
        index,
        location.y,
        visible.y,
    )
    # Check pointer seeking independently of the follow animation. Pick a
    # fully visible character inside the viewport rather than a line edge.
    page._follow_until = 2**63 - 1
    visible = view.get_visible_rect()
    candidates = []
    _found, iterator = view.get_iter_at_location(0, visible.y + 24)
    for a, _b, word in view.ranges[bisect_left(view.offsets, iterator.get_offset()) :]:
        location = view.get_iter_location(view.buffer.get_iter_at_offset(a))
        if visible.y + 24 <= location.y and location.y + location.height < visible.y + visible.height - 24:
            candidates.append((a, word, location))
            break
    assert candidates, "No fully visible word available for native pointer input"
    offset, word, location = candidates[0]
    x, y = view.buffer_to_window_coords(
        Gtk.TextWindowType.WIDGET, location.x + max(1, location.width // 2), location.y + location.height // 2
    )
    seeks = []
    original_seek = page.player.seek
    page.player.seek = seeks.append
    try:
        point = Graphene.Point()
        point.init(x, y)
        ok, point = view.compute_point(window, point)
        assert ok
        script = f"""(() => {{
            Main.overview.hide();
            const a = global.get_window_actors().find(a => a.meta_window.title === {json.dumps(window.get_title())});
            a.meta_window.activate(global.get_current_time());
            const r = a.meta_window.get_frame_rect();
            const C = imports.gi.Clutter;
            if (!global._textviewPointer)
                global._textviewPointer = C.get_default_backend().get_default_seat().create_virtual_device(C.InputDeviceType.POINTER_DEVICE);
            const d = global._textviewPointer, t = imports.gi.GLib.get_monotonic_time();
            d.notify_absolute_motion(t, r.x + {point.x}, r.y + {point.y});
            d.notify_button(t + 10000, 1, C.ButtonState.PRESSED);
            d.notify_button(t + 60000, 1, C.ButtonState.RELEASED);
            return 'ok';
        }})()"""
        completed = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell",
                "--object-path",
                "/org/gnome/Shell",
                "--method",
                "org.gnome.Shell.Eval",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert completed.stdout.startswith("(true,"), completed.stdout
        dispatch_until(lambda: seeks)
        assert seeks == [word.start_ms], ("click seek", seeks, word.start_ms)
        view.buffer.place_cursor(view.buffer.get_iter_at_offset(offset))
        from gi.repository import Gdk

        assert view._key(None, Gdk.KEY_Return, 0, 0)
        assert seeks == [word.start_ms, word.start_ms]
    finally:
        page.player.seek = original_seek
    print("PASS follow, native GNOME pointer click and keyboard seek", flush=True)
    if os.environ.get("CASSINI_TEXTVIEW_SCREENSHOT"):
        subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell.Screenshot",
                "--object-path",
                "/org/gnome/Shell/Screenshot",
                "--method",
                "org.gnome.Shell.Screenshot.Screenshot",
                "false",
                "false",
                os.environ["CASSINI_TEXTVIEW_SCREENSHOT"],
            ],
            check=True,
            capture_output=True,
            timeout=5,
        )


def check(window, meeting, name):
    subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.Shell",
            "--object-path",
            "/org/gnome/Shell",
            "--method",
            "org.gnome.Shell.Eval",
            "Main.overview.hide(); true",
        ],
        check=True,
        capture_output=True,
        timeout=5,
    )
    window.back_to_library()
    paint(window)
    frame = FrameCPU(window)
    start_frame = window.get_frame_clock().get_frame_time()
    started = time.thread_time_ns()
    window.open_meeting(meeting, external=True)
    open_cpu = (time.thread_time_ns() - started) / 1e6
    page = window.meeting_page
    view = page.textview
    page._follow_until = 2**63 - 1
    paint(window)
    assert view.get_mapped() and view.get_width() > 0
    first = (window.get_frame_clock().get_frame_time() - start_frame) / 1000
    adjustment = page.scroller.get_vadjustment()
    # GtkTextView's lower-priority incremental validator can run outside
    # before/after-paint. Include CPU between paints and wait on a watermark
    # below its priority before calling the whole buffer laid out.
    uppers = [adjustment.get_upper()]
    upper_handler = adjustment.connect("changed", lambda *_: uppers.append(adjustment.get_upper()))
    layout_idle(window)
    ready = (window.get_frame_clock().get_frame_time() - start_frame) / 1000
    adjustment.disconnect(upper_handler)
    uppers.append(adjustment.get_upper())
    end_before = time.thread_time_ns()
    end_rect = view.get_iter_location(view.buffer.get_end_iter())
    header_x = view.get_iter_location(view.buffer.get_start_iter()).x
    word_x = view.get_iter_location(view.buffer.get_iter_at_offset(view.ranges[0][0])).x
    assert header_x == 32, ("paragraph gutter", header_x)
    assert word_x == (252 if meeting.speakers else 32), ("body tab", word_x)
    validation_cpu = (time.thread_time_ns() - end_before) / 1e6
    load_cpu = max(frame.samples)
    load_between = max(frame.between_paints)
    frame.samples.clear()
    frame.between_paints.clear()
    controllers = page.scroller.observe_controllers()
    candidates = [controllers.get_item(i) for i in range(controllers.get_n_items())]
    controllers = [c for c in candidates if isinstance(c, Gtk.EventControllerScroll)]
    controller = next(
        c
        for c in controllers
        if c.get_flags() & Gtk.EventControllerScrollFlags.KINETIC
        and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    )
    bubble = next(
        c
        for c in controllers
        if c.get_flags() & Gtk.EventControllerScrollFlags.KINETIC
        and c.get_propagation_phase() == Gtk.PropagationPhase.BUBBLE
    )
    assert page.scroller.get_kinetic_scrolling()
    print("native controllers", [(str(c.get_flags()), str(c.get_propagation_phase())) for c in controllers], flush=True)
    if os.environ.get("CASSINI_TEXTVIEW_CONTROLS_ONLY"):
        before = window.get_width()
        window.set_default_size(960, 650)
        if before != 960:
            dispatch_until(lambda: window.get_width() != before)
        layout_idle(window)
        check_controls(window, page)
        return dict(controls=True)
    # Test every visible word's seek identity, including each folded reply.
    assert len(view.ranges) == sum(
        bool(value) for row in meeting.turns for owner in (row, *(a for _, a in row.asides)) for value in owner.items
    )
    assert all(view.word_at_offset(a) is word and view.word_at_offset(b - 1) is word for a, b, word in view.ranges)
    assert len(view.ranges) == len(meeting.words)
    tag = view.current
    forward = sorted({word.start_ms for _a, _b, word in view.ranges})
    highlight_cpu = []
    for position in forward[:1000]:
        tick = time.thread_time_ns()
        view.highlight(position)
        highlight_cpu.append((time.thread_time_ns() - tick) / 1e6)
    # Check overlap highlights against the original timed ranges after both
    # forward jumps and backward seeks; a tag must never alter text geometry.
    for position in (meeting.duration_ms // 2, 1000, meeting.duration_ms // 3):
        view.highlight(position)
        expected = {i for i, (_a, _b, w) in enumerate(view.ranges) if w.start_ms <= position < w.end_ms}
        assert view._active == expected
        assert all(view.buffer.get_iter_at_offset(view.ranges[i][0]).has_tag(tag) for i in expected)
    scroll_reports = []
    raw = []
    for playing in (False, True):
        page.player.seek(1000)
        if playing:
            page.player.play()
            dispatch_until(lambda: page.player.playing)
        else:
            page.player.pause()
        page._follow_until = 2**63 - 1
        # Begin away from either boundary. Never replace GTK's input handler.
        adjustment.set_value((adjustment.get_upper() - adjustment.get_page_size()) * 0.35)
        paint(window)
        samples, reversals, upper_changes = [], 0, 0
        frame.samples.clear()
        frame.between_paints.clear()
        stable_upper = adjustment.get_upper()
        for flick in range(6):
            direction = 1 if flick % 2 == 0 else -1
            previous = anchor(view)
            controller.emit("scroll-begin")
            bubble.emit("scroll-begin")
            # GTK's surface unit is wheel steps for signal emission without
            # a GdkEvent; its native handler scales these into pixel deltas.
            for delta in (0.8, 1.2, 1.8, 2.4, 1.8):
                if not controller.emit("scroll", 0.0, direction * delta):
                    bubble.emit("scroll", 0.0, direction * delta)
                paint(window)
                current = anchor(view)
                reversals += direction * (current[0] - previous[0]) < 0 or (
                    current[0] == previous[0] and direction * (current[1] - previous[1]) < -1
                )
                samples.append((flick, "finger", current, adjustment.get_value(), adjustment.get_upper()))
                previous = current
            controller.emit("decelerate", 0.0, direction * 35.0)
            controller.emit("scroll-end")
            bubble.emit("scroll-end")
            # Sample all frames for 1.5s of frame-clock time; no wall sleeps.
            coast_start = window.get_frame_clock().get_frame_time()
            while window.get_frame_clock().get_frame_time() - coast_start < 1_500_000:
                paint(window)
                current = anchor(view)
                reversals += direction * (current[0] - previous[0]) < 0 or (
                    current[0] == previous[0] and direction * (current[1] - previous[1]) < -1
                )
                samples.append((flick, "coast", current, adjustment.get_value(), adjustment.get_upper()))
                previous = current
            # Require real inertia, beyond finger displacement.
            finger = next(s for s in reversed(samples) if s[1] == "finger")
            assert direction * (samples[-1][3] - finger[3]) > 1, ("no native coast", name, flick)
        assert reversals == 0, (name, playing, "visible anchor reversed", reversals)
        if playing:
            assert page.player.position > 2000, "Playback never advanced during the scrolling test"
        upper_changes = sum(abs(sample[4] - stable_upper) > 0.5 for sample in samples)
        scroll_reports.append(
            dict(
                playing=playing,
                finalPlaybackPositionMs=page.player.position,
                frames=len(samples),
                reversals=int(reversals),
                framesUpperChanged=upper_changes,
                upperMin=min(s[4] for s in samples),
                upperMax=max(s[4] for s in samples),
                frameCpuMaxMs=max(frame.samples),
                betweenPaintCpuMaxMs=max(frame.between_paints),
            )
        )
        raw.append(samples)
        page.player.pause()
    frame.samples.clear()
    frame.between_paints.clear()
    size_before = (window.get_width(), window.get_height(), view.get_width())
    window.set_default_size(960, 650)
    dispatch_until(lambda: window.get_width() != size_before[0])
    layout_idle(window)
    size_after = (window.get_width(), window.get_height(), view.get_width())
    resize_cpu = max(frame.samples)
    resize_between = max(frame.between_paints)
    result = dict(
        name=name,
        words=len(meeting.words),
        rows=len(meeting.turns),
        chips=sum(len(r.asides) for r in meeting.turns),
        firstPaintFrameMs=first,
        layoutObservationFrameMs=ready,
        openCpuMs=open_cpu,
        endValidationCpuMs=validation_cpu,
        endY=end_rect.y,
        loadFrameCpuMaxMs=load_cpu,
        loadBetweenPaintCpuMaxMs=load_between,
        loadUpperMin=min(uppers),
        loadUpperMax=max(uppers),
        highlightCpuMeanMs=sum(highlight_cpu) / len(highlight_cpu),
        highlightCpuMaxMs=max(highlight_cpu),
        resizeFrameCpuMaxMs=resize_cpu,
        resizeBetweenPaintCpuMaxMs=resize_between,
        sizeBefore=size_before,
        sizeAfter=size_after,
        rssMiB=rss(),
        scroll=scroll_reports,
    )
    print(json.dumps(result), flush=True)
    frame.close()
    checkpoint = Path(os.environ["CASSINI_TEXTVIEW_OUTPUT"]).with_name(name + "-checkpoint.json")
    checkpoint.write_text(json.dumps(dict(report=result, samples=raw, loadUppers=uppers), indent=2))
    check_controls(window, page)
    window.set_default_size(1120, 700)
    return dict(report=result, samples=raw, loadUppers=uppers)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    import subprocess

    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas), GSETTINGS_BACKEND="memory", CASSINI_LIBRARY_DIR=str(root / "Library")
    )
    app = CassiniApplication("com.gocassini.TextViewSpike", "test", "development")
    app.transcription_enabled = False
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.unmaximize()
    Gio.Settings.new(app.settings_schema).set_boolean("companion-card-dismissed", True)
    window.set_default_size(1120, 700)
    dispatch_until(lambda: not window._pending)
    window.monitor.callback = lambda *_: None
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player._pipeline.set_property("audio-sink", sink)
    results = []
    for hours in map(int, os.environ.get("CASSINI_TEXTVIEW_HOURS", "1,4").split(",")):
        if hours == 4:
            available = int(
                next(
                    line.split()[1]
                    for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:")
                )
            )
            assert available > 4000 * 1024, "Need >4000 MiB available before the heavy fixture"
        opus = hour_meeting(root / f"Meeting-{hours}", mixed=True, hours=hours)
        meeting = load_open_meeting(opus, root / "Library")
        if not os.environ.get("CASSINI_TEXTVIEW_PLAIN_ONLY"):
            results.append(check(window, meeting, f"{hours}h-diarized"))
            Path(os.environ["CASSINI_TEXTVIEW_OUTPUT"]).write_text(json.dumps(results, indent=2))
        if hours == 1:
            # Same finished synthetic audio/text, with speaker attribution
            # removed before projecting reading rows: no chips or over labels.
            plain_words = tuple(replace(word, speaker="unattributed") for word in meeting.words)
            plain = replace(
                meeting,
                words=plain_words,
                turns=reading_turns(plain_words),
                listening_turns=speaker_turns(plain_words),
                speakers=(),
            )
            results.append(check(window, plain, "1h-plain"))
            Path(os.environ["CASSINI_TEXTVIEW_OUTPUT"]).write_text(json.dumps(results, indent=2))
    Path(os.environ["CASSINI_TEXTVIEW_OUTPUT"]).write_text(json.dumps(results, indent=2))
    app.on_quit()
    app.do_shutdown()
print("PASS TextView prototype measurements", flush=True)
