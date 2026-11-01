# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only PCM sources on one seekable meeting timeline, mixed by GStreamer.

Each appsrc exposes the *meeting* timeline. A seek changes its PCM read cursor
and the silence before its known offset; it never assumes an unknown offset is
zero. This lets audiomixer forward the same seek to every source, including
sources that start later or have already ended. No intermediate audio is written.
A compacted Opus span (S-12 §5) is decoded at 48 kHz by track_source's
seekable decoder instead of being read from its WAV.
"""

import os
import threading

from gi.repository import GLib, Gst

from .library import _wave
from .player import Player
from .track_source import OpusDecoder

CHUNK_FRAMES = 2048


class PCMSource:
    def __init__(self, track):
        if track.start_offset_ms is None:
            raise ValueError("Cannot align a track with an unknown start offset")
        from .mapped_audio import required

        self.track = track
        self.children = [PCMSource(span) for span in getattr(track, "spans", ())]
        self.mapped = (
            bool(self.children)
            or bool(getattr(track, "clock_anchors", ()))
            and required(dict(sampleRate=track.sample_rate, clockAnchors=track.clock_anchors))
        )
        self.mapping = None
        self._mapped_cache = None
        self.path = track.path
        self.fd = None
        self.codec = track.codec
        self.decoder = None
        self.frames, self.rate, self.channels = track.pcm_frames, track.pcm_rate, track.channels
        self.offset_ns = round(track.start_offset_ms * Gst.MSECOND)
        self.leading_frames = (self.offset_ns * self.rate + Gst.SECOND - 1) // Gst.SECOND
        self.end_frame = self.leading_frames + self.frames
        if self.children:
            self.rate = 48000
            self.end_frame = max(round(child.end_frame * 48000 / child.rate) for child in self.children)
        elif self.mapped:
            from .capture_clock import mapped_end_sample

            end = mapped_end_sample(
                track.frames,
                track.sample_rate,
                track.start_offset_ms,
                track.clock_anchors,
                decoded_frames=track.pcm_frames if track.codec == "opus" else None,
            )
            self.end_frame = max(0, round(end * self.rate / 48000))
        from .source_ownership import FrameMask

        self.ownership = FrameMask(getattr(track, "ownership_windows", None), self.rate)
        self.cursor = 0
        self.lock = threading.Lock()
        self.src = Gst.ElementFactory.make("appsrc", None)
        self.src.set_property(
            "caps",
            Gst.Caps.from_string(
                f"audio/x-raw,format={'F32LE' if self.mapped else 'S16LE'},layout=interleaved,rate={self.rate},channels={self.channels}"
            ),
        )
        self.src.set_property("format", Gst.Format.TIME)
        self.src.set_property("stream-type", 1)  # GST_APP_STREAM_TYPE_SEEKABLE, no GstApp typelib dependency.
        self.src.set_property("duration", self.end_frame * Gst.SECOND // self.rate)
        self.handlers = [self.src.connect("need-data", self._need), self.src.connect("seek-data", self._seek)]

    def _seek(self, _src, ns):
        with self.lock:
            self.cursor = min(self.end_frame, (ns * self.rate + Gst.SECOND - 1) // Gst.SECOND)
        return True

    def _open(self):
        # appsrc calls need-data on its streaming thread, including this initial IO.
        if self.children or self.fd is not None or self.decoder is not None:
            return
        if self.codec == "opus":
            decoder = OpusDecoder(self.path)
            if decoder.info.samples < self.frames or decoder.channels != self.channels:
                decoder.close()
                raise ValueError("Recording source changed after the Library scan")
            self.decoder = decoder
            return
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            with os.fdopen(os.dup(fd), "rb") as stream:
                frames, rate, channels = _wave(stream, os.fstat(fd))
            if frames < self.frames or (rate, channels) != (self.rate, self.channels):
                raise ValueError("Recording source changed after the Library scan")
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def _need(self, src, _length):
        try:
            with self.lock:
                self._open()
                cursor = self.cursor
                frames = min(CHUNK_FRAMES, self.end_frame - cursor)
                if frames <= 0:
                    src.emit("end-of-stream")
                    return
                if self.mapped:
                    data = self._mapped_data(cursor, frames)
                else:
                    silence = min(frames, max(0, self.leading_frames - cursor))
                    audio_frames = frames - silence
                    data = bytes(silence * self.channels * 2)
                    if audio_frames and self.decoder is not None:
                        self.decoder.seek(max(0, cursor - self.leading_frames))
                        data += self.decoder.read(audio_frames)
                        if len(data) != frames * self.channels * 2:
                            raise ValueError("Incomplete recording source")
                    elif audio_frames:
                        data += os.pread(
                            self.fd,
                            audio_frames * self.channels * 2,
                            44 + max(0, cursor - self.leading_frames) * self.channels * 2,
                        )
                        if len(data) != frames * self.channels * 2:
                            raise ValueError("Incomplete recording source")
                data = self.ownership.pcm(data, cursor, self.channels * (4 if self.mapped else 2))
                self.cursor += frames
        except (OSError, ValueError, TimeoutError, GLib.Error) as error:
            message = GLib.Error.new_literal(Gst.ResourceError.quark(), str(error), Gst.ResourceError.READ)
            src.post_message(Gst.Message.new_error(src, message, str(error)))
            src.emit("end-of-stream")
            return
        buffer = Gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        buffer.pts = cursor * Gst.SECOND // self.rate
        buffer.duration = (cursor + frames) * Gst.SECOND // self.rate - buffer.pts
        src.emit("push-buffer", buffer)

    def _mapped_segment(self, first, end):
        """A bounded native-rate window; sequential Opus reads retain their overlap."""
        import numpy as np

        values = np.zeros((0, self.channels), dtype=np.float64)
        start = first
        if self._mapped_cache is not None:
            base, cached = self._mapped_cache
            if base <= first < base + len(cached):
                stop = min(end, base + len(cached))
                values = cached[first - base : stop - base]
                start = stop
        if start < end:
            count = end - start
            if self.decoder is not None:
                self.decoder.seek(start)
                data = self.decoder.read(count)
            else:
                data = os.pread(self.fd, count * self.channels * 2, 44 + start * self.channels * 2)
            if len(data) != count * self.channels * 2:
                raise ValueError("Incomplete recording source")
            extra = np.frombuffer(data, dtype="<i2").reshape(-1, self.channels).astype(np.float64) / 32768
            values = np.concatenate((values, extra))
        self._mapped_cache = first, values
        return values

    def _mapped_data(self, cursor, count, *, output_rate=None):
        """Use the publisher's clock map, preserving channels and never touching the WAV."""
        import numpy as np

        from .library_v2.mixdown import Mapping, Span
        from .track_source import SpanInfo

        output_rate = output_rate or self.rate
        if self.children:
            output = np.zeros((count, self.channels), dtype=np.float64)
            for child in self.children:
                first = child.track.start_offset_ms * 48
                end = child.end_frame * 48000 / child.rate
                if cursor + count > first and cursor < end:
                    child._open()
                    output += np.frombuffer(child._mapped_data(cursor, count, output_rate=48000), dtype="<f4").reshape(
                        -1, self.channels
                    )
                else:
                    child._release_input()
            return output.astype("<f4").tobytes()
        if self.mapping is None:
            track = self.track
            info = SpanInfo(
                self.codec, (self.path.name,), track.frames, track.sample_rate, self.channels, self.frames, self.rate
            )
            self.mapping = Mapping(
                Span(getattr(track, "id", "source"), self.path, info, track.start_offset_ms, track.clock_anchors)
            )
        output = np.zeros((count, self.channels), dtype=np.float64)
        source = self.mapping.source(np.arange(cursor, cursor + count, dtype=np.float64) * 48000 / output_rate)
        source *= self.rate / 48000
        inside = (source >= 0) & (source <= self.frames - 1)
        if inside.any():
            positions = source[inside]
            low = np.floor(positions).astype(np.int64)
            first, end = int(low.min()), min(self.frames, int(low.max()) + 2)
            segment = self._mapped_segment(first, end)
            segment = np.concatenate((segment, np.zeros((1, self.channels))))
            fraction = (positions - low)[:, None]
            index = low - first
            output[inside] = segment[index] * (1 - fraction) + segment[index + 1] * fraction
        return output.astype("<f4").tobytes()

    def close(self):
        for child in self.children:
            child.close()
        for handler in self.handlers:
            self.src.disconnect(handler)
        self._release_input()

    def _release_input(self):
        self._mapped_cache = self.mapping = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.decoder is not None:
            self.decoder.close()
            self.decoder = None


class FolderPlayer(Player):
    """Player's transport properties, with PCM appsrc branches instead of playbin."""

    __gtype_name__ = "CassiniFolderPlayer"

    def __init__(self, *, audio_sink=None):
        super().__init__()
        self._not_loaded = self._load_error
        super().shutdown()
        self.sources = []
        self.audio_sink = audio_sink
        self._pipeline = None
        self._bus_handler = None

    @staticmethod
    def can_play(meeting):
        return (
            any(t.frames > 0 for t in meeting.tracks)
            and meeting.lifecycle == "saved"
            and all(t.start_offset_ms is not None for t in meeting.tracks)
        )

    def load_tracks(self, tracks, duration_ms=None):
        self.shutdown()
        if not tracks:
            raise ValueError("No recording sources")
        pipeline = Gst.Pipeline.new(None)
        self._pipeline = pipeline
        mixer = Gst.ElementFactory.make("audiomixer", None)
        convert = Gst.ElementFactory.make("audioconvert", None)
        tempo = Gst.ElementFactory.make("scaletempo", None)
        sink = self.audio_sink or Gst.ElementFactory.make("autoaudiosink", None)
        if not all((mixer, convert, tempo, sink)):
            raise RuntimeError("GStreamer audio mixing elements are unavailable")
        for element in (mixer, convert, tempo, sink):
            pipeline.add(element)
        if not (mixer.link(convert) and convert.link(tempo) and tempo.link(sink)):
            raise RuntimeError("Could not link the audio mixer")
        try:
            for track in tracks:
                source = PCMSource(track)
                self.sources.append(source)
                convert = Gst.ElementFactory.make("audioconvert", None)
                resample = Gst.ElementFactory.make("audioresample", None)
                queue = Gst.ElementFactory.make("queue", None)
                for element in (source.src, convert, resample, queue):
                    pipeline.add(element)
                if not (
                    source.src.link(convert) and convert.link(resample) and resample.link(queue) and queue.link(mixer)
                ):
                    raise RuntimeError("Could not link a recording source")
        except Exception:
            self.shutdown()
            raise
        self.duration = (
            duration_ms if duration_ms is not None else max(s.end_frame * 1000 // s.rate for s in self.sources)
        )
        self.position = 0
        self._pending_seek = None
        self._prerolled = False
        self._at_end = False
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_message)
        self._load_error = None
        pipeline.set_state(Gst.State.PAUSED)

    def stop(self):
        if self._pipeline is not None:
            super().stop()

    def shutdown(self):
        if self._pipeline is not None:
            self.stop()
            bus = self._pipeline.get_bus()
            if self._bus_handler:
                bus.disconnect(self._bus_handler)
                bus.remove_signal_watch()
                self._bus_handler = None
            self._pipeline = None
        for source in self.sources:
            source.close()
        self.sources.clear()
        self._load_error = self._not_loaded
        self._prerolled = False
        self._pending_seek = None
