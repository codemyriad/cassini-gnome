#!/usr/bin/env python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned-desktop runner; commands and frame gates, no sleeps or scroll interception."""

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).parent)]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--fixture", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--mode", choices=("listbox", "listview"), default="listbox")
parser.add_argument("--container", choices=("listbox", "box"), default="listbox")
parser.add_argument("--fast-measure", choices=("0", "1"), default="1")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
os.environ.update(
    CASSINI_TRANSCRIPT_RENDER=args.mode,
    CASSINI_LISTBOX_FAST_MEASURE=args.fast_measure,
    CASSINI_LISTBOX_CONTAINER=args.container,
    GSETTINGS_SCHEMA_DIR=str(ROOT / "data"),
    GSETTINGS_BACKEND="memory",
    GSK_RENDERER="cairo",
    CASSINI_LIBRARY_DIR=str(args.output / "empty-library"),
)
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(ROOT / "cassini-talk.gresource")))
from probe import FrameProbe  # noqa: E402

from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_page import MeetingPage  # noqa: E402
from cassini_talk.meeting_text import reading_word  # noqa: E402
from cassini_talk.open_meeting import load_open_meeting  # noqa: E402

probe_holder = []
highlight = []
original_position = MeetingPage._position


def timed_position(page, *a):
    started = time.thread_time_ns()
    try:
        return original_position(page, *a)
    finally:
        highlight.append(
            dict(
                phase=probe_holder[0].phase if probe_holder else "opening",
                cpuMs=(time.thread_time_ns() - started) / 1e6,
            )
        )


MeetingPage._position = timed_position
app = CassiniApplication("com.gocassini.RenderListbox" + str(os.getpid()), "spike", "development")
app.transcription_enabled = False
app.register(None)
app.activate()
window = app.get_active_window()
context = GLib.MainContext.default()
while window._pending:
    context.iteration(True)
window.monitor.callback = lambda *_: None
meeting = load_open_meeting(args.fixture, args.fixture.parent)
window.player._pipeline.set_property("audio-sink", Gst.ElementFactory.make("fakesink", None))
window.player._pipeline.get_property("audio-sink").set_property("sync", True)
opened = time.monotonic_ns()
open_cpu = time.thread_time_ns()
window.open_meeting(meeting, external=True)
page = window.meeting_page
window.set_default_size(1280, 768)
window.present()


def anchor():
    if args.mode == "listbox":
        row = page.list.get_row_at_y(round(page.scroller.get_vadjustment().get_value())) or page.list.get_row_at_index(
            0
        )
        if row is None or row.get_height() <= 0:
            return None
        ok, b = row.compute_bounds(page.scroller)
        return (row.get_index(), max(0, -b.get_y())) if ok else None
    visible = []
    for index, (_turn, _words, row) in page.bound.items():
        parent = row.get_parent()
        if not row.get_mapped() or not parent.get_child_visible():
            continue
        ok, b = parent.compute_bounds(page.scroller)
        if ok and b.get_height() > 0 and b.get_y() < page.scroller.get_height() and b.get_y() + b.get_height() > 0:
            visible.append((index, max(0, -b.get_y())))
    return min(visible) if visible else None


def loaded():
    return (
        (page.list.built and (not page.list.items or page.list.items[-1].child.get_height() > 0))
        if args.mode == "listbox"
        else bool(page.bound)
    )


probe = FrameProbe(page.scroller, anchor, loaded=loaded)
probe.open_ns = opened
probe.interval_cpu = open_cpu
probe_holder.append(probe)
ready_printed = [False]
stable = [None, 0]


def ready(*_):
    upper = page.scroller.get_vadjustment().get_upper()
    stable[1] = stable[1] + 1 if stable[0] == upper else 0
    stable[0] = upper
    if loaded() and stable[1] >= 2 and not ready_printed[0]:
        ready_printed[0] = True
        probe.phase = "ready"
        (args.output / "ready.json").write_text(
            json.dumps(
                dict(
                    words=len(meeting.words),
                    rows=len(meeting.turns),
                    chips=sum(len(t.asides) for t in meeting.turns),
                    over=sum(bool(t.over) for t in meeting.turns),
                    appId=app.get_application_id(),
                )
            )
        )
        dump("load.json")
        print("READY", app.get_application_id(), flush=True)
    return not ready_printed[0]


page.add_tick_callback(ready)


def dump(name):
    probe.dump(
        args.output / name,
        model=dict(words=len(meeting.words), rows=len(meeting.turns), namedSpeakers=len(meeting.speakers)),
        renderer=args.mode,
        fastMeasure=args.fast_measure,
        highlightCpuMs=highlight,
        buildChunkCpuMs=page.list.build_cpu_ms if args.mode == "listbox" else [],
        visibleRefreshCpuMs=page.list.visible_refresh_cpu_ms if args.mode == "listbox" else [],
    )


control = args.output / "control"
if not control.exists():
    os.mkfifo(control)
fd = os.open(control, os.O_RDWR | os.O_NONBLOCK)


def verify_features():
    anchors = 0
    spots = []
    for index, (turn, words, _row) in page.bound.items():
        links = ET.fromstring("<root>" + words.get_label() + "</root>").findall(".//a")
        for link in links:
            uri = link.attrib["href"]
            word = reading_word(turn, uri)
            assert word in turn.words or any(word in aside.words for _, aside in turn.asides)
            anchors += 1
        if links and len(spots) < 2:
            uri = links[0].attrib["href"]
            spots.append((index, words, uri, reading_word(turn, uri)))
        if not any("aside:" in spot[2] for spot in spots):
            for link in links:
                if link.attrib["href"].startswith("aside:"):
                    uri = link.attrib["href"]
                    spots.append((index, words, uri, reading_word(turn, uri)))
                    break
    assert anchors == len(meeting.words), (anchors, len(meeting.words))
    results = dict(
        wordLinks=anchors,
        words=len(meeting.words),
        sidebarSpeakers=len(page._speaker_rows),
        checkedSeeks=[],
        chips=sum(len(t.asides) for t in meeting.turns),
    )

    def next_spot():
        if not spots:
            (args.output / "features.json").write_text(json.dumps(results))
            return GLib.SOURCE_REMOVE
        index, words, uri, word = spots.pop(0)
        page.list.scroll_to(index, Gtk.ListScrollFlags.NONE, None)
        if args.mode == "listbox":
            page.list.refresh_position()

        def reached(*_):
            if window.player.position != word.start_ms:
                return
            window.player.disconnect(handler)
            assert "background=" in page.bound[index][1].get_label(), "Current word was not highlighted"
            results["checkedSeeks"].append(dict(row=index, uri=uri, position=window.player.position, highlight=True))
            GLib.idle_add(next_spot)

        handler = window.player.connect("notify::position", reached)
        if window.player.position == word.start_ms:
            window.player.seek(word.start_ms + 1)
        assert words.emit("activate-link", uri)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(next_spot)


def command(*_):
    try:
        message = os.read(fd, 4096).decode().strip()
    except BlockingIOError:
        return True
    pieces = message.split()
    op = pieces[0] if pieces else ""
    if op == "phase":
        probe.phase = pieces[1]
    elif op == "reset":
        page.list.scroll_to(min(125, len(meeting.turns) // 4), Gtk.ListScrollFlags.NONE, None)
    elif op == "pause":
        window.player.pause()
    elif op == "play":
        page._follow_until = 2**62
        window.player.seek(300000)
        window.player.play()
    elif op == "follow":
        page._follow_until = 0
        page._reading_index = -1
        page._position()
    elif op == "resize":
        probe.phase = "resize"
        window.unmaximize()
        window.set_default_size(1000 if len(pieces) < 2 else int(pieces[1]), 700)
    elif op == "settle":
        previous = [None, 0]
        target = pieces[1]

        def painted(*_):
            adjustment = page.scroller.get_vadjustment()
            state = (adjustment.get_value(), adjustment.get_upper(), anchor())
            previous[1] = previous[1] + 1 if previous[0] == state else 0
            previous[0] = state
            if previous[1] >= 3:
                clock.disconnect(signal)
                (args.output / target).write_text(json.dumps(dict(us=GLib.get_monotonic_time(), state=state)))

        clock = page.scroller.get_frame_clock()
        signal = clock.connect("after-paint", painted)

        def keep_painting(*_):
            return previous[1] < 3

        page.add_tick_callback(keep_painting)
    elif op == "features":
        probe.phase = "features"
        verify_features()
    elif op == "dump":
        dump(pieces[1] if len(pieces) > 1 else "result.json")
    elif op == "quit":
        dump("final.json")
        app.quit()
    (args.output / "last-command.json").write_text(json.dumps(dict(command=message, us=GLib.get_monotonic_time())))
    return True


GLib.io_add_watch(fd, GLib.IO_IN, command)
app.run([])
