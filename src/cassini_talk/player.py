# player.py
#
# Copyright 2026 Codemyriad
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Audio playback with GStreamer playbin, exposed as GObject properties."""

from gi.repository import GLib, GObject, Gst

from . import runtime
from .i18n import gettext as _

TICK_MS = 40
_CONSUMED = object()  # An EOS tag already taken: no later EOS message can match it.


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
        # playbin, not playbin3: under load about 1 in 100 playbin3 prerolls hung
        # in parsebin's state change, blocking load (D70 #242 review); playbin
        # did not hang in 480. runtime.py already requires only playbin.
        self._pipeline = Gst.ElementFactory.make("playbin", None)
        if self._pipeline is None:
            raise RuntimeError("GStreamer playbin is not available")
        self._pipeline.set_property("video-sink", Gst.ElementFactory.make("fakesink", None))
        # scaletempo keeps the pitch natural when playing faster or slower; an
        # identity stands in without it, so the stream always passes an element
        # of ours (_watch_stream).
        audio_filter = Gst.ElementFactory.make("scaletempo", None) or Gst.ElementFactory.make("identity", None)
        self._pipeline.set_property("audio-filter", audio_filter)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_message)
        self._tick_id = 0
        self._prerolled = False
        self._pending_seek: int | None = None
        self._at_end = False
        self._range_end = None
        # Match both the EOS event's own identity and its segment. The mixer
        # can give EOS a new seqnum, but the downstream event and bus message
        # share that identity. Matching only the latest segment can misread a
        # queued old message after the new EOS has already reached the filter.
        self._seek_seqnum = self._flush_seqnum = self._eos_seqnum = None
        self._watch_stream(audio_filter)
        # D70 listening: (end_ms, on_done) of the span in progress.
        self._span = None
        # The pipeline's segment no longer matches what play() should do (drained at a
        # span's stop, or a stop that was cleared while paused): play() seeks first.
        self._stale_segment = False
        # While listening, called with every seek target; returns the (end_ms, on_done) that
        # target plays to, or None to play freely. A dialog sample (play_range) takes precedence.
        self.span_follow = None
        # Why play() cannot start, or None once a file is loaded.
        self._load_error: str | None = _("No recording is loaded.")

    @property
    def loaded(self) -> bool:
        return self._load_error is None

    def load(self, uri: str, duration_ms: int | None = None):
        self.stop()
        self._prerolled = False
        self._pending_seek = None
        self._at_end = False
        self._seek_seqnum = self._flush_seqnum = self._eos_seqnum = None
        self.position = 0
        missing = runtime.missing_gst_elements(Gst.ElementFactory.find, runtime.playback_elements(uri))
        if missing:
            # playbin would only say "missing plug-in"; name what to install.
            # Forget the previous file too, so play() cannot fall back to it.
            self._load_error = _("Missing GStreamer elements: {elements}. Install {plugins}.").format(
                elements=", ".join(missing), plugins=runtime.GST_PLUGINS
            )
            self._pipeline.set_property("uri", "")
            self.duration = 0
            self.emit("error", self._load_error)
            return
        self._load_error = None
        self.duration = duration_ms or 0
        self._pipeline.set_property("uri", uri)
        self._pipeline.set_state(Gst.State.PAUSED)

    def play(self):
        if not self.loaded:
            self.emit("error", self._load_error)
            return
        if self._at_end:
            self.seek(0)
        elif self._stale_segment:
            # Continue from the position, under the stop span_follow decides (if any).
            self.seek(self.position)
        # Preroll must apply the segment before any audio can reach the sink.
        waiting = not self._prerolled and (
            self._pending_seek is not None or self._stop_ms() is not None or self.rate != 1.0
        )
        state = Gst.State.PAUSED if waiting else Gst.State.PLAYING
        if self._pipeline.set_state(state) == Gst.StateChangeReturn.FAILURE:
            # The bus usually holds the reason; take it so it is reported once.
            message = self._pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            text = message.parse_error()[0].message if message else _("The audio could not be played.")
            self.stop()
            self.emit("error", text)
            return
        self.playing = True
        self._start_tick()

    def play_range(self, start_ms, end_ms):
        """Play a voice sample with a GStreamer stop boundary, at most 10 s."""
        start_ms = max(0, int(start_ms))
        end_ms = min(int(end_ms), start_ms + 10000)
        if self.duration:
            end_ms = min(end_ms, self.duration)
        if not self.loaded or not self._prerolled or end_ms <= start_ms:
            return False
        self.pause()
        self._range_end = end_ms
        if not self.seek(start_ms):
            self.clear_range()
            self.emit("error", _("The audio could not be played."))
            return False
        self.play()
        return self.playing

    def clear_range(self):
        self._range_end = None

    def play_span(self, start_ms, end_ms, on_done=None):
        """D70: play [start, end] and pause exactly at end; on_done(end_ms) follows.

        Unlike play_range there is no length cap and no position restore: the
        position stays at end. The stop is the seek's segment stop; the EOS it
        produces is posted only after the sink has rendered the last sample, so
        a 0.3 s turn needs no position polling. (SEGMENT_DONE would arrive when
        the source has merely pushed the span, long before it is heard.)
        """
        start_ms = max(0, int(start_ms))
        end_ms = int(end_ms)
        if self.duration:
            end_ms = min(end_ms, self.duration)
        if not self.loaded:
            self.emit("error", self._load_error)
            return False
        if end_ms <= start_ms:
            return False
        self.clear_range()
        self._span = (end_ms, on_done)
        self._stale_segment = False
        self._at_end = False
        self.position = start_ms
        if not self._prerolled:
            self._pending_seek = start_ms
        elif not self._do_seek(start_ms):
            self.clear_span()
            self.emit("error", _("The audio could not be played."))
            return False
        self.play()
        return self.playing

    def clear_span(self):
        """Forget the span and listening follow; playing on, if playing, without a stop."""
        had_stop = self._span is not None
        self._span = None
        self.span_follow = None
        if had_stop and self._range_end is None and self._prerolled:
            if self.playing:
                self._do_seek(self._query_position() or self.position)
            else:
                self._stale_segment = True

    def pause(self):
        if not self.loaded:
            return
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
        self.clear_range()
        self._span = None
        self._stale_segment = False
        self.span_follow = None
        self._stop_tick()
        self._pipeline.set_state(Gst.State.NULL)
        self.playing = False

    def seek(self, ms: int):
        ms = max(0, int(ms))
        if self.duration:
            ms = min(ms, self.duration)
        self._at_end = False
        self._stale_segment = False
        if self._range_end is None:
            if self.span_follow is not None:
                self._span = self.span_follow(ms)
            elif self._span is not None and ms >= self._span[0]:
                self._span = None
        # Let the listening controller classify the seek before its position
        # notification can overwrite the last non-sample listening position.
        self.position = ms
        if not self._prerolled:
            self._pending_seek = ms
            return True
        return self._do_seek(ms)

    def skip(self, delta_ms: int):
        self.seek(self.position + delta_ms)

    def set_rate(self, rate: float):
        self.rate = rate
        if not self._prerolled:
            return
        position = self._query_position() or self.position
        stop = self._stop_ms()
        drained = self._eos_seqnum is _CONSUMED or (
            isinstance(self._eos_seqnum, tuple) and self._eos_seqnum[1] == self._seek_seqnum
        )
        if self._stale_segment or self._at_end or drained or (stop is not None and position >= stop):
            # The span has rendered to its stop and its EOS is on its way: a seek
            # now would drop that EOS and play on past the turn. play() applies
            # the rate when it continues.
            self._stale_segment = True
            return
        self._do_seek(position)

    def _stop_ms(self):
        if self._range_end is not None:
            return self._range_end
        return self._span[0] if self._span is not None else None

    def _do_seek(self, ms: int):
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        stop = self._stop_ms()
        if stop is not None and stop <= ms:
            # A stop at or before the start is an invalid segment: already there, so it is done.
            stop = None
        event = Gst.Event.new_seek(
            self.rate,
            Gst.Format.TIME,
            flags,
            Gst.SeekType.SET,
            ms * Gst.MSECOND,
            Gst.SeekType.SET,
            stop * Gst.MSECOND if stop is not None else -1,
        )
        # A consumed EOS belongs to the previous segment, not the seek we
        # are about to send. The streaming probe tags any new EOS afresh.
        self._eos_seqnum = None
        sent = self._pipeline.send_event(event)
        if sent:
            self._seek_seqnum = event.get_seqnum()
        return sent

    def _watch_stream(self, element):
        """Tag each EOS event with the seek whose FLUSH_STOP preceded it (streaming thread)."""

        def watch(_pad, info):
            event = info.get_event()
            if event.type == Gst.EventType.FLUSH_STOP:
                self._flush_seqnum = event.get_seqnum()
            elif event.type == Gst.EventType.EOS:
                self._eos_seqnum = event.get_seqnum(), self._flush_seqnum
            return Gst.PadProbeReturn.OK

        element.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.EVENT_FLUSH, watch)

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
        if not self._prerolled or self._pending_seek is not None or self._stale_segment or self._at_end:
            return
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
                if self._span is not None and self.duration:
                    self._span = min(self._span[0], self.duration), self._span[1]
                if self._pending_seek is not None or self._stop_ms() is not None or self.rate != 1.0:
                    self._do_seek(self._pending_seek or 0)
                    self._pending_seek = None
                if self.playing:
                    self._pipeline.set_state(Gst.State.PLAYING)
        elif message.type == Gst.MessageType.DURATION_CHANGED:
            ok, dur = self._pipeline.query_duration(Gst.Format.TIME)
            if ok and dur > 0 and not self.duration:
                self.duration = dur // Gst.MSECOND
        elif message.type == Gst.MessageType.EOS:
            if self._eos_seqnum != (message.get_seqnum(), self._seek_seqnum):
                return  # The end of a segment a later seek replaced.
            self._eos_seqnum = _CONSUMED
            self._pipeline.set_state(Gst.State.PAUSED)
            self.playing = False
            self._stop_tick()
            if self._range_end is None and self._span is not None:
                end_ms, on_done = self._span
                self._span = None
                self._stale_segment = True
                self._at_end = bool(self.duration) and end_ms >= self.duration
                self.position = end_ms
                if on_done is not None:
                    on_done(end_ms)
                return
            self._at_end = True
            if self.duration:
                self.position = self._range_end if self._range_end is not None else self.duration
        elif message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            self.stop()
            self.emit("error", err.message)
