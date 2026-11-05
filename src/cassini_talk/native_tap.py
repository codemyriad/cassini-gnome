# SPDX-License-Identifier: GPL-3.0-or-later
"""One independent native source, on a monotonic clock, without a GTK main loop."""

import logging
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .recorder import CAPTURE_CONVERT, TAP_QUEUE  # noqa: E402
from .recording_details import ACMeter  # noqa: E402
from .refusals import gst_code  # noqa: E402

# The microphone must deliver its first buffer within this bound (R79 §1).
# D58.1: for the others' side (computer sound) it is only the window in which an
# explicit error refuses the tap at its open; silence never does.
OPEN_TIMEOUT_S = 3
# Stop drains the tap: EOS follows every buffer still queued (up to 10 s of
# audio after a host stall, TAP_QUEUE) through the writer.
DRAIN_TIMEOUT_S = 10
LOG = logging.getLogger(__name__)
NO_SAMPLE = "Native audio capture did not produce a sample"


class CaptureError(OSError):
    """An explicit capture failure, with the D58 code it is refused with."""

    def __init__(self, message, refusal_code="deviceUnavailable"):
        super().__init__(message)
        self.refusal_code = refusal_code


class NativeTap:
    """`session_clock` is the capture's SessionClock: its one mapping of the session
    origin onto CLOCK_MONOTONIC (D61). The tap reads no wall clock."""

    def __init__(self, kind, session_clock, append, failed, *, level=lambda _: None, test=False):
        Gst.init(None)
        self.kind, self.session_clock = kind, session_clock
        self.append, self.failed, self.level = append, failed, level
        self.test = test
        self.pipeline = None
        self.ended = threading.Event()
        self.ready = threading.Event()
        # D58.1: a first buffer was delivered (ready is also set by an error).
        self.heard = threading.Event()
        self.closed = threading.Event()
        self.error = None
        self.refusal_code = "deviceUnavailable"
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
            source + " ! " + TAP_QUEUE + " ! " + CAPTURE_CONVERT + " ! audioresample ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
            "appsink name=pcm emit-signals=true sync=false max-buffers=8 drop=false"
        )
        self.clock = Gst.SystemClock.obtain()
        self.pipeline.use_clock(self.clock)
        # The pipeline clock read together with the monotonic clock: its times
        # reach the session timeline through the capture's one mapping (D61).
        before = time.monotonic_ns()
        self.anchor_clock = self.clock.get_time()
        self.anchor_session = self.session_clock.at((before + time.monotonic_ns()) // 2)
        self.pipeline.get_by_name("pcm").connect("new-sample", self._sample)
        self.thread = threading.Thread(target=self._messages, name="cassini-" + self.kind, daemon=True)
        self.thread.start()
        try:
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise CaptureError("Could not start native audio capture")
            # D58.1: a silent computer is not a failure: the others' tap stays
            # armed and delivers its first buffer whenever sound comes.
            if not self.ready.wait(OPEN_TIMEOUT_S) and self.kind != "system":
                raise OSError(NO_SAMPLE)
            if self.error:
                raise CaptureError(self.error, self.refusal_code)
        except BaseException:
            self.closed.set()
            self.pipeline.set_state(Gst.State.NULL)
            self.thread.join(timeout=1)
            raise

    def _fail(self, error, gerror=None):
        if self.error is None and not self.closed.is_set():
            self.error = str(error)
            self.refusal_code = gst_code(gerror)
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
            offset = self.anchor_session + (base + buffer.pts - self.anchor_clock) / Gst.MSECOND
            data = buffer.extract_dup(0, buffer.get_size())
            if self._ac_meter.feed(data):
                self.level(self._ac_meter.level)
            self.append(data, offset, host_ns=base + buffer.pts)
            self.heard.set()
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
                    error = message.parse_error()[0]
                    self._fail(error.message, error)
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
        # A tap that never delivered a buffer (D58.1: a silent computer) has
        # nothing to drain: Pulse may never finish the read EOS would wait on.
        if not self.heard.is_set():
            self.ended.set()
        if not self.ended.is_set():

            def eos():
                if not pipeline.send_event(Gst.Event.new_eos()):
                    self.error = self.error or "Could not finish native audio capture"
                    self.ended.set()

            sender = threading.Thread(target=eos, name="cassini-source-eos", daemon=True)
            sender.start()
        complete = self.ended.wait(DRAIN_TIMEOUT_S)
        if not complete:
            queue = pipeline.get_by_name("tap-queue")
            LOG.warning(
                "Native %s tap did not drain within %d s; %s ms of queued audio was not saved",
                self.kind,
                DRAIN_TIMEOUT_S,
                "unknown" if queue is None else queue.get_property("current-level-time") // Gst.MSECOND,
            )
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
