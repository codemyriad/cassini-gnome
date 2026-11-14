# SPDX-License-Identifier: GPL-3.0-or-later
"""D-977 guided test phrase: the default microphone, 16 kHz mono float, held in memory only."""

import logging
import sys
import threading
from array import array

from gi.repository import GLib, GObject

from .voice_enroll import MAX_ENROL_MS, RATE, _dbfs

LOG = logging.getLogger(__name__)


IDLE, STARTING, LIVE, CLOSING, CLOSED = "idle", "starting", "live", "closing", "closed"
CANCELLED = "cancelled"
# R79 §1, as NativeRecorder's OPEN_TIMEOUT_S: the microphone has opened when
# audio arrives within this bound; otherwise the take ends as unavailable.
OPEN_TIMEOUT_S = 3.0


class PhraseRecorder(GObject.Object):
    """Up to MAX_ENROL_MS of the default microphone; nothing is written to disk here.

    Signals: `level(dBFS)` while listening, `finished(ok)` once stopped (ok is
    False when the microphone could not be opened or failed mid-take).

    Every GStreamer state change runs on a worker thread, never on GTK: a
    sound server that accepts the connection but never answers would
    otherwise freeze the window on Record, Stop or Cancel. `start`, `stop`
    and `cancel` return at once, and `finished` follows on the main loop as
    soon as the take is settled (the samples are sealed then), without
    waiting for GStreamer. Stop, Cancel or the open bound while the
    microphone is still opening ends the take there; the worker closes the
    microphone to NULL as soon as it unblocks, so it is never left live.
    """

    __gsignals__ = {
        "level": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        "finished": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    # The same element as recordings (runtime.RECORDING_ELEMENTS), on the default microphone.
    SOURCE = 'pulsesrc client-name="Cassini voice sample" provide-clock=false'

    open_timeout_s = OPEN_TIMEOUT_S

    def __init__(self, source=None, open_timeout_s=None):
        super().__init__()
        self.source = source or self.SOURCE
        if open_timeout_s is not None:
            self.open_timeout_s = open_timeout_s
        self.samples = array("f")
        self.pipeline = None
        self._phase = IDLE
        self._done = False
        self._outcome = None
        self._lock = threading.Lock()

    @property
    def listening(self):
        with self._lock:
            return not self._done and self._phase in (STARTING, LIVE)

    def start(self):
        """GTK thread: returns at once; the microphone opens on a worker thread."""
        from gi.repository import Gst

        Gst.init(None)
        with self._lock:
            if self._phase != IDLE:
                return
            self._phase = STARTING
            self.samples = array("f")
        GLib.timeout_add(max(1, round(self.open_timeout_s * 1000)), self._open_timed_out)
        threading.Thread(target=self._open, name="cassini-voice-sample-open", daemon=True).start()

    def _open_timed_out(self):
        """GTK thread: no audio within the open bound means the microphone is unavailable."""
        with self._lock:
            stalled = not self._done and not len(self.samples)
        if stalled:
            LOG.warning("The voice sample microphone did not open within %.1f s", self.open_timeout_s)
            self._end(False)
        return False

    def _open(self):
        """Worker thread: build the pipeline and wait out PLAYING, however long it blocks."""
        from gi.repository import Gst

        pipeline, ok = None, False
        try:
            pipeline = Gst.parse_launch(
                f"{self.source} ! audioconvert ! audioresample ! "
                f"audio/x-raw,format=F32LE,rate={RATE},channels=1,layout=interleaved ! "
                "appsink name=sink emit-signals=true sync=false max-buffers=64 drop=false"
            )
            pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
            bus = pipeline.get_bus()
            bus.add_signal_watch()  # Dispatched on the GTK main context.
            bus.connect("message::error", lambda *_: self._end(False))
            bus.connect("message::eos", lambda *_: self._end(True))
            ok = pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        except Exception:  # A worker that dies here would leave the dialog listening forever.
            LOG.exception("Could not open the voice sample microphone")
        with self._lock:
            if ok and not self._done:
                self._phase, self.pipeline = LIVE, pipeline
                return
        if not ok:
            self._end(False)  # PLAYING failed: unavailable, never a too-short take (no-op once settled).
        self._close(pipeline)  # Stopped, cancelled, timed out or failed: tear down in the background.

    def _on_sample(self, sink):
        """Streaming thread: keep the chunk under the lock, so EOS never overtakes it."""
        from gi.repository import Gst

        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            chunk = array("f")
            chunk.frombytes(bytes(info.data)[: len(info.data) // 4 * 4])
        finally:
            buffer.unmap(info)
        if sys.byteorder != "little":
            chunk.byteswap()
        with self._lock:
            if self._done:
                return Gst.FlowReturn.EOS
            room = MAX_ENROL_MS * RATE // 1000 - len(self.samples)
            self.samples.extend(chunk[:room])
            full = room <= len(chunk)
        if len(chunk):
            GLib.idle_add(self._level, _dbfs(chunk, 0, len(chunk)))
        if full:
            GLib.idle_add(self.stop)
            return Gst.FlowReturn.EOS
        return Gst.FlowReturn.OK

    def _level(self, dbfs):
        if not self._done:
            self.emit("level", dbfs)
        return False

    def stop(self):
        """Keep the take; `finished(True)` follows once the microphone is closed."""
        self._end(True)
        return False

    def cancel(self):
        """Stop without a result; the take is dropped and `finished` is never emitted."""
        self._end(CANCELLED)

    def _end(self, outcome):
        """Any thread, never blocks: settle the take once, report it, close the microphone off GTK."""
        with self._lock:
            if outcome is CANCELLED:
                self.samples = array("f")
            if self._done:
                if outcome is CANCELLED:
                    self._outcome = CANCELLED  # A result still in flight is dropped too.
                return
            self._done, self._outcome = True, outcome
            phase, pipeline = self._phase, self.pipeline
            if phase in (IDLE, LIVE):
                self._phase, self.pipeline = CLOSING, None
        # `self.samples` is sealed now (`_on_sample` drops everything once `_done`),
        # so GTK hears the result at once, however long GStreamer takes to close.
        GLib.idle_add(self._report)
        if phase == LIVE:
            threading.Thread(
                target=self._close, args=(pipeline,), name="cassini-voice-sample-close", daemon=True
            ).start()
        # STARTING: the opening worker sees `_done` once PLAYING returns and closes it.

    def _close(self, pipeline):
        """Worker thread: NULL joins the streaming thread; never touches the settled take."""
        if pipeline is None:
            return
        from gi.repository import Gst

        try:
            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            LOG.exception("Could not close the voice sample microphone")

    def _report(self):
        """GTK thread, once per take: queued only by whoever set `_done`."""
        with self._lock:
            self._phase, outcome = CLOSED, self._outcome
        if outcome is not CANCELLED:
            self.emit("finished", outcome)
        return False
