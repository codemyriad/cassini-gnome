# SPDX-License-Identifier: GPL-3.0-or-later
"""Live capture on one GStreamer clock, finalized before exposing the file."""

import os
import tempfile
import threading
from gettext import gettext as _

from gi.repository import GLib, GObject, Gst


def capture_pipeline(microphone=True, desktop=True, test=False):
    if not microphone and not desktop:
        raise ValueError("Select at least one audio source")
    sources = []
    for enabled, device in ((microphone, "@DEFAULT_SOURCE@"), (desktop, "@DEFAULT_MONITOR@")):
        if enabled:
            index = len(sources)
            source = (
                f"audiotestsrc is-live=true freq={440 + index * 440}"
                if test
                else f'pulsesrc device={device} client-name="Cassini Recording" provide-clock=false'
            )
            sources.append(
                f"{source} ! queue ! audioconvert ! audioresample ! "
                f"audio/x-raw,format=S16LE,rate=48000,channels=1 ! capture.sink_{index}"
            )
    return (
        "audiointerleave name=capture ! audioconvert ! opusenc bitrate=128000 ! "
        "oggmux ! filesink name=output " + " ".join(sources)
    )


def request_eos(pipeline, rejected):
    """End each independent source without taking stream locks on GTK.

    Serial EOS delivery through a bin can wait on another interleave input.
    Give each source its own sender so all inputs can finish independently.
    The bus remains the authority for completed output; the existing watchdog
    handles sources that do not respond. A late rejection belongs to this
    pipeline only, never a subsequent recording.
    """
    iterator = pipeline.iterate_sources()
    sources = []
    while True:
        result, source = iterator.next()
        if result == Gst.IteratorResult.OK:
            sources.append(source)
        elif result == Gst.IteratorResult.RESYNC:
            iterator.resync()
            sources.clear()
        else:
            break

    def send(source):
        try:
            handled = source.send_event(Gst.Event.new_eos())
        except Exception:
            handled = False
        if not handled:
            GLib.idle_add(rejected, pipeline)

    for source in sources or [pipeline]:
        threading.Thread(target=send, args=(source,), name="cassini-capture-eos", daemon=True).start()


class Recorder(GObject.Object):
    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self):
        super().__init__()
        self.pipeline = None
        self._timeout = 0
        self._temporary = None
        self.stopping = False

    def start(self, path, microphone=True, desktop=True, *, test=False):
        if self.pipeline is not None:
            raise RuntimeError("Already recording")
        self.path = os.fspath(path)
        self.stopping = False
        try:
            self.pipeline = Gst.parse_launch(capture_pipeline(microphone, desktop, test))
            self.pipeline.use_clock(Gst.SystemClock.obtain())
            fd, self._temporary = tempfile.mkstemp(
                prefix=".cassini-", suffix=".opus", dir=os.path.dirname(os.path.abspath(path))
            )
            os.close(fd)
            self.pipeline.get_by_name("output").set_property("location", self._temporary)
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            self._handler = bus.connect("message", self._message)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError(_("Could not start audio capture. Check your audio devices and GStreamer plugins."))
        except Exception:
            self._cleanup()
            raise

    def stop(self):
        if self.pipeline is None or self.stopping:
            return
        self.stopping = True
        self._timeout = GLib.timeout_add_seconds(10, self._timed_out)
        request_eos(self.pipeline, self._eos_rejected)

    def _eos_rejected(self, pipeline):
        if self.pipeline is pipeline and self.stopping:
            self._fail(_("Could not finish the recording."))
        return False

    def _timed_out(self):
        self._timeout = 0
        self._fail(_("Timed out while saving the recording."))
        return GLib.SOURCE_REMOVE

    def _message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self._fail(error.message)
        elif message.type == Gst.MessageType.EOS:
            self.pipeline.set_state(Gst.State.NULL)
            try:
                os.replace(self._temporary, self.path)
                self._temporary = None
            except OSError as error:
                self._fail(str(error))
                return
            self._cleanup()
            self.emit("finished", self.path)

    def _fail(self, message):
        # Preserve captured pages on a device/disk error. Never replace the user's
        # chosen destination with a file that did not finalize successfully.
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        if self._temporary and os.path.exists(self._temporary) and os.path.getsize(self._temporary):
            recovery = self._temporary
            try:
                fd, recovery = tempfile.mkstemp(
                    prefix=os.path.basename(self.path) + ".interrupted-",
                    suffix=".opus",
                    dir=os.path.dirname(os.path.abspath(self.path)),
                )
                os.close(fd)
                os.replace(self._temporary, recovery)
            except OSError:
                recovery = self._temporary
            self._temporary = None
            message += " " + _("Incomplete audio was kept at {path}.").format(path=recovery)
        self._cleanup()
        self.emit("error", message)

    def _cleanup(self):
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            bus = self.pipeline.get_bus()
            if hasattr(self, "_handler"):
                bus.disconnect(self._handler)
                del self._handler
                bus.remove_signal_watch()
            self.pipeline = None
        if self._temporary:
            os.unlink(self._temporary)
            self._temporary = None
        self.stopping = False
