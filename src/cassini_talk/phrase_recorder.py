# SPDX-License-Identifier: GPL-3.0-or-later
"""D-977 guided test phrase: the default microphone, 16 kHz mono float, held in memory only."""

import logging
import sys
import threading
from array import array

from gi.repository import GLib, GObject

from .voice_enroll import MAX_ENROL_MS, RATE, _dbfs

LOG = logging.getLogger(__name__)


class PhraseRecorder(GObject.Object):
    """Up to MAX_ENROL_MS of the default microphone; nothing is written to disk here.

    Signals: `level(dBFS)` while listening, `finished(ok)` once stopped (ok is
    False when the microphone could not be opened or failed mid-take).
    """

    __gsignals__ = {
        "level": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        "finished": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    # The same element as recordings (runtime.RECORDING_ELEMENTS), on the default microphone.
    SOURCE = 'pulsesrc client-name="Cassini voice sample" provide-clock=false'

    def __init__(self, source=None):
        super().__init__()
        self.source = source or self.SOURCE
        self.samples = array("f")
        self.pipeline = None
        self._done = False
        self._lock = threading.Lock()

    @property
    def listening(self):
        return self.pipeline is not None

    def start(self):
        from gi.repository import Gst

        Gst.init(None)
        self.samples = array("f")
        self._done = False
        try:
            self.pipeline = Gst.parse_launch(
                f"{self.source} ! audioconvert ! audioresample ! "
                f"audio/x-raw,format=F32LE,rate={RATE},channels=1,layout=interleaved ! "
                "appsink name=sink emit-signals=true sync=false max-buffers=64 drop=false"
            )
        except GLib.Error:
            LOG.exception("Could not build the voice sample pipeline")
            self.pipeline = None
            GLib.idle_add(self._finish, False)
            return
        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda *_: self._finish(False))
        bus.connect("message::eos", lambda *_: self._finish(True))
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            GLib.idle_add(self._finish, False)

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
        self._finish(True)
        return False

    def _finish(self, ok):
        with self._lock:
            if self._done:
                return False
            self._done = True
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            from gi.repository import Gst

            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)  # Joins the streaming thread.
        self.emit("finished", ok)
        return False

    def cancel(self):
        """Stop without a result; the take is dropped."""
        with self._lock:
            self._done = True
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            from gi.repository import Gst

            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)  # Joins the streaming thread.
        self.samples = array("f")
