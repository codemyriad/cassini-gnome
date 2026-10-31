# SPDX-License-Identifier: GPL-3.0-or-later
"""One independent native source, on a monotonic clock, without a GTK main loop."""

import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .recording_details import ACMeter  # noqa: E402


class NativeTap:
    def __init__(self, kind, origin_ms, append, failed, *, level=lambda _: None, test=False):
        Gst.init(None)
        self.kind, self.origin_ms = kind, origin_ms
        self.append, self.failed, self.level = append, failed, level
        self.test = test
        self.pipeline = None
        self.ended = threading.Event()
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.error = None
        self.thread = None
        self._ac_meter = ACMeter()

    def start(self):
        device = "@DEFAULT_SOURCE@" if self.kind == "microphone" else "@DEFAULT_MONITOR@"
        source = (
            f"audiotestsrc is-live=true freq={660 if self.kind == 'microphone' else 880}"
            if self.test
            else f'pulsesrc name=capture-source device={device} provide-clock=false client-name="Cassini Recording"'
        )
        self.pipeline = Gst.parse_launch(
            source + " ! queue ! audioconvert ! audioresample ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
            "appsink name=pcm emit-signals=true sync=false max-buffers=8 drop=false"
        )
        self.clock = Gst.SystemClock.obtain()
        self.pipeline.use_clock(self.clock)
        self.anchor_clock = self.clock.get_time()
        self.anchor_wall = time.time_ns() / 1_000_000
        self.pipeline.get_by_name("pcm").connect("new-sample", self._sample)
        self.thread = threading.Thread(target=self._messages, name="cassini-" + self.kind, daemon=True)
        self.thread.start()
        try:
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise OSError("Could not start native audio capture")
            if not self.ready.wait(3):
                raise OSError("Native audio capture did not produce a sample")
            if self.error:
                raise OSError(self.error)
        except BaseException:
            self.closed.set()
            self.pipeline.set_state(Gst.State.NULL)
            self.thread.join(timeout=1)
            raise

    def _fail(self, error):
        if self.error is None and not self.closed.is_set():
            self.error = str(error)
            self.ready.set()
            self.failed(self.error)

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        try:
            base = self.pipeline.get_base_time()
            if buffer.pts == Gst.CLOCK_TIME_NONE or base == Gst.CLOCK_TIME_NONE:
                raise ValueError("Audio source has no clock timestamp")
            offset = self.anchor_wall - self.origin_ms + (base + buffer.pts - self.anchor_clock) / Gst.MSECOND
            data = buffer.extract_dup(0, buffer.get_size())
            if self._ac_meter.feed(data):
                self.level(self._ac_meter.level)
            self.append(data, offset, host_ns=base + buffer.pts)
            self.ready.set()
        except Exception as error:
            self._fail(error)
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def _messages(self):
        bus = self.pipeline.get_bus()
        kinds = Gst.MessageType.ERROR | Gst.MessageType.EOS
        try:
            while not self.closed.is_set():
                message = bus.timed_pop_filtered(100 * Gst.MSECOND, kinds)
                if message is None:
                    continue
                if message.type == Gst.MessageType.ERROR:
                    self._fail(message.parse_error()[0].message)
                    self.ended.set()
                    return
                if message.type == Gst.MessageType.EOS:
                    self.ended.set()
                    return
        except Exception as error:
            self._fail(error)
            self.ended.set()

    def stop(self):
        pipeline = self.pipeline
        if pipeline is None:
            return
        # EOS can block while Pulse finishes a read. Neither GTK nor a session
        # writer lock waits inside send_event; NULL also releases that sender.
        sender = None
        if not self.ended.is_set():

            def eos():
                if not pipeline.send_event(Gst.Event.new_eos()):
                    self.error = self.error or "Could not finish native audio capture"
                    self.ended.set()

            sender = threading.Thread(target=eos, name="cassini-source-eos", daemon=True)
            sender.start()
        complete = self.ended.wait(10)
        self.closed.set()
        pipeline.set_state(Gst.State.NULL)
        if self.thread is not None:
            self.thread.join(timeout=1)
        if sender is not None:
            sender.join(timeout=1)
        self.pipeline = None
        if not complete:
            raise TimeoutError("Timed out while saving native audio")
        if self.error:
            raise OSError(self.error)
