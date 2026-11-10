# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Renderer-neutral observation only: no scroll handling or momentum changes."""
import json
import resource
import time
from pathlib import Path

from gi.repository import GLib, Gtk


def rss_kib():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


class FrameProbe:
    """anchor() returns (logical row/character offset, within-item pixel offset)."""
    def __init__(self, scroller, anchor, *, loaded=lambda:True):
        self.scroller, self.anchor, self.loaded = scroller, anchor, loaded
        self.frames, self.input, self.ticks = [], [], []
        self.phase = "opening"
        self.open_ns = time.monotonic_ns()
        self.baseline_rss = rss_kib()
        self.first_rows_ns = self.ready_ns = None
        self.direction = 0
        self.clock = None
        self.paint_cpu = self.interval_cpu = time.thread_time_ns()
        self.observer = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES,
                                                 propagation_phase=Gtk.PropagationPhase.CAPTURE)
        self.observer.connect("scroll", self._input)
        scroller.add_controller(self.observer)
        scroller.connect("map", self._mapped)
        if scroller.get_mapped(): self._mapped()

    def _input(self, _controller, dx, dy):
        if dy: self.direction=1 if dy>0 else -1
        self.input.append(dict(us=GLib.get_monotonic_time(),dx=dx,dy=dy,direction=self.direction))
        return False

    def _mapped(self,*_):
        if self.clock is not None:return
        self.clock=self.scroller.get_frame_clock()
        self.clock.connect("before-paint",self._before)
        self.clock.connect("after-paint",self._after)

    def _before(self,*_): self.paint_cpu=time.thread_time_ns()

    def _after(self,*_):
        cpu=time.thread_time_ns();now=time.monotonic_ns()
        anchor=self.anchor()
        if anchor is not None and self.first_rows_ns is None:self.first_rows_ns=now
        if anchor is not None and self.loaded() and self.ready_ns is None:self.ready_ns=now
        adjustment=self.scroller.get_vadjustment()
        self.frames.append(dict(us=GLib.get_monotonic_time(),phase=self.phase,direction=self.direction,
                                anchor=anchor,value=adjustment.get_value(),upper=adjustment.get_upper(),
                                paintCpuMs=(cpu-self.paint_cpu)/1e6,intervalCpuMs=(cpu-self.interval_cpu)/1e6))
        self.interval_cpu=cpu

    def measure(self,kind,callback,*args):
        before=time.thread_time_ns()
        try:return callback(*args)
        finally:self.ticks.append(dict(kind=kind,cpuMs=(time.thread_time_ns()-before)/1e6))

    def dump(self,path,**extra):
        report=dict(firstRowsMs=(self.first_rows_ns-self.open_ns)/1e6 if self.first_rows_ns else None,
                    builtAndPaintedMs=(self.ready_ns-self.open_ns)/1e6 if self.ready_ns else None,
                    baselineRssKiB=self.baseline_rss,rssKiB=rss_kib(),maxRssKiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    nativeKinetic=self.scroller.get_kinetic_scrolling(),frames=self.frames,input=self.input,ticks=self.ticks,**extra)
        Path(path).write_text(json.dumps(report))
        return report
