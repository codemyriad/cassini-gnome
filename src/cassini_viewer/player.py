# player.py
#
# Copyright 2026 Silvio Tomatis
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Audio playback with GStreamer playbin, exposed as GObject properties."""

from gettext import gettext as _

from gi.repository import GLib, GObject, Gst

from . import runtime

TICK_MS = 40


class Player(GObject.Object):
    """Plays one file. Times are in milliseconds.

    *position* updates about 25 times a second while playing, which is what
    the transcript highlight follows.
    """

    __gtype_name__ = "CassiniPlayer"

    __gsignals__ = {
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    position = GObject.Property(type=GObject.TYPE_INT64, default=0)
    duration = GObject.Property(type=GObject.TYPE_INT64, default=0)
    playing = GObject.Property(type=bool, default=False)
    rate = GObject.Property(type=float, default=1.0)

    def __init__(self):
        super().__init__()
        self._pipeline = Gst.ElementFactory.make("playbin3", None) or Gst.ElementFactory.make("playbin", None)
        if self._pipeline is None:
            raise RuntimeError("GStreamer playbin is not available")
        self._pipeline.set_property("video-sink", Gst.ElementFactory.make("fakesink", None))
        scaletempo = Gst.ElementFactory.make("scaletempo", None)
        if scaletempo is not None:
            # Keeps the pitch natural when playing faster or slower.
            self._pipeline.set_property("audio-filter", scaletempo)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_message)
        self._tick_id = 0
        self._prerolled = False
        self._pending_seek: int | None = None
        self._at_end = False

    def load(self, uri: str, duration_ms: int | None = None):
        self.stop()
        self._prerolled = False
        self._at_end = False
        self.duration = duration_ms or 0
        self.position = 0
        missing = runtime.missing_gst_elements(Gst.ElementFactory.find)
        if missing:
            # playbin would only say "missing plug-in"; name what to install.
            self.emit(
                "error",
                _("Missing GStreamer elements: {elements}. Install {plugins}.").format(
                    elements=", ".join(missing), plugins=runtime.GST_PLUGINS
                ),
            )
            return
        self._pipeline.set_property("uri", uri)
        self._pipeline.set_state(Gst.State.PAUSED)

    def play(self):
        if self._at_end:
            self.seek(0)
        self._pipeline.set_state(Gst.State.PLAYING)
        self.playing = True
        self._start_tick()

    def pause(self):
        self._pipeline.set_state(Gst.State.PAUSED)
        self.playing = False
        self._stop_tick()
        self._update_position()

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        self._stop_tick()
        self._pipeline.set_state(Gst.State.NULL)
        self.playing = False

    def seek(self, ms: int):
        ms = max(0, int(ms))
        if self.duration:
            ms = min(ms, self.duration)
        self._at_end = False
        self.position = ms
        if not self._prerolled:
            self._pending_seek = ms
            return
        self._do_seek(ms)

    def skip(self, delta_ms: int):
        self.seek(self.position + delta_ms)

    def set_rate(self, rate: float):
        self.rate = rate
        if self._prerolled:
            self._do_seek(self._query_position() or self.position)

    def _do_seek(self, ms: int):
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        self._pipeline.seek(self.rate, Gst.Format.TIME, flags, Gst.SeekType.SET, ms * Gst.MSECOND, Gst.SeekType.NONE, 0)

    def shutdown(self):
        self.stop()
        bus = self._pipeline.get_bus()
        bus.disconnect(self._bus_handler)
        bus.remove_signal_watch()

    def _start_tick(self):
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(TICK_MS, self._on_tick)

    def _stop_tick(self):
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _on_tick(self):
        self._update_position()
        return GLib.SOURCE_CONTINUE

    def _query_position(self) -> int | None:
        ok, pos = self._pipeline.query_position(Gst.Format.TIME)
        return pos // Gst.MSECOND if ok and pos >= 0 else None

    def _update_position(self):
        pos = self._query_position()
        if pos is not None and pos != self.position:
            self.position = pos

    def _on_message(self, _bus, message):
        if message.type == Gst.MessageType.ASYNC_DONE:
            if not self._prerolled:
                self._prerolled = True
                if not self.duration:
                    ok, dur = self._pipeline.query_duration(Gst.Format.TIME)
                    if ok and dur > 0:
                        self.duration = dur // Gst.MSECOND
                if self._pending_seek is not None or self.rate != 1.0:
                    self._do_seek(self._pending_seek or 0)
                    self._pending_seek = None
        elif message.type == Gst.MessageType.DURATION_CHANGED:
            ok, dur = self._pipeline.query_duration(Gst.Format.TIME)
            if ok and dur > 0 and not self.duration:
                self.duration = dur // Gst.MSECOND
        elif message.type == Gst.MessageType.EOS:
            self._pipeline.set_state(Gst.State.PAUSED)
            self.playing = False
            self._stop_tick()
            self._at_end = True
            if self.duration:
                self.position = self.duration
        elif message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            self.stop()
            self.emit("error", err.message)
