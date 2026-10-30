# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §5: one reader for a session span, a v1 WAV or a compacted Opus file.

A span in session.json is either:

- PCM WAV: no ``codec``; ``file`` is a ``track-*.wav`` basename (v1, unchanged);
- compacted Opus: ``codec: "opus"``, ``file: "tracks/<span-id>.opus"`` and
  ``decodedRate: 48000``. ``sampleRate``/``frames`` stay the original source
  metadata; the decoded stream is 48 kHz after pre-skip and end trim.

Every path resolves inside the session folder only, one component at a time
with O_NOFOLLOW, to a regular file. Opus lengths are compared as time in
48 kHz samples, never as raw frame counts at different rates.

No GStreamer or NumPy import happens at module load: the Library scanner and
GTK use only the pure parts; decoding imports GStreamer lazily.
"""

import errno
import os
import stat
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .library_files import basename, regular
from .portable import ogg_crc

OPUS_RATE = 48000
# One 20 ms Opus frame at 48 kHz: the §5 length tolerance.
OPUS_FRAME = 960
# Decoded and discarded before a seek target (RFC 7845 asks for >= 80 ms).
PREROLL = OPUS_RATE // 4
TRACKS = "tracks"
MAX_FRAMES = 9007199254740991
NOFOLLOW_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class SpanInfo:
    """What a reader may rely on: source metadata plus what the decoder yields."""

    codec: str  # "pcm" (WAV) or "opus"
    parts: tuple[str, ...]  # path components relative to the session folder
    frames: int  # source frames at sample_rate (WAV: complete frames on disk)
    sample_rate: int
    channels: int  # channels the decoder yields
    decoded_frames: int  # frames the decoder yields at decoded_rate
    decoded_rate: int

    @property
    def relative(self):
        return "/".join(self.parts)


@dataclass(frozen=True)
class OpusInfo:
    samples: int  # playable 48 kHz samples: final granule minus pre-skip
    channels: int
    pre_skip: int


def span_reference(value):
    """(codec, parts) for one session.json span; ValueError for anything unsafe."""
    if not isinstance(value, dict):
        raise ValueError("invalid-track")
    codec = value.get("codec")
    name = value.get("file")
    if codec is None:
        name = basename(name)
        if not name.startswith("track-") or not name.endswith(".wav"):
            raise ValueError("invalid-track-name")
        return "pcm", (name,)
    if codec != "opus":
        raise ValueError("unsupported-track-codec")
    if value.get("decodedRate") != OPUS_RATE:
        raise ValueError("unsupported-decoded-rate")
    if not isinstance(name, str) or name.count("/") != 1:
        raise ValueError("unsafe-reference")
    folder, leaf = name.split("/")
    leaf = basename(leaf)
    if folder != TRACKS or leaf.startswith(".") or not leaf.endswith(".opus") or len(leaf) <= len(".opus"):
        raise ValueError("invalid-track-name")
    return "opus", (TRACKS, leaf)


@contextmanager
def open_span(directory_fd, parts):
    """Yield (stream, stat) for a span relative to an open session folder."""
    if len(parts) == 1:
        with regular(directory_fd, parts[0]) as opened:
            yield opened
        return
    folder = os.open(basename(parts[0]), NOFOLLOW_DIRECTORY, dir_fd=directory_fd)
    try:
        with regular(folder, parts[1]) as opened:
            yield opened
    finally:
        os.close(folder)


def open_track(path, codec="pcm"):
    """A read-only fd for a scanned track path, re-checked without following links."""
    path = Path(path)
    if codec == "pcm":
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    elif codec == "opus":
        if path.parent.name != TRACKS:
            raise ValueError("unsafe-reference")
        session = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            folder = os.open(TRACKS, NOFOLLOW_DIRECTORY, dir_fd=session)
        finally:
            os.close(session)
        try:
            fd = os.open(basename(path.name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
        finally:
            os.close(folder)
    else:
        raise ValueError("unsupported-track-codec")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not-a-regular-file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def wave_header(stream, info):
    """(complete frames on disk, rate, channels) of a v1 PCM16 WAV."""
    header = stream.read(44)
    if len(header) != 44:
        raise ValueError("incomplete-wav-header")
    riff, _, wave, fmt, size, encoding, channels, rate, byte_rate, alignment, bits, data, _ = struct.unpack(
        "<4sI4s4sIHHIIHH4sI", header
    )
    if (
        riff != b"RIFF"
        or wave != b"WAVE"
        or fmt != b"fmt "
        or data != b"data"
        or size != 16
        or encoding != 1
        or bits != 16
        or channels not in (1, 2)
        or not 8000 <= rate <= 192000
        or alignment != channels * 2
        or byte_rate != rate * channels * 2
    ):
        raise ValueError("unsupported-pcm-header")
    return (info.st_size - 44) // alignment, rate, channels


def final_samples(stream, head, info):
    """Granule minus pre-skip of the final complete EOS page (reads <= 64 KiB), or None."""
    if len(head) < 19:
        return None
    stream.seek(0)
    first = stream.read(27)
    serial = first[14:18]
    stream.seek(max(0, info.st_size - 65536))
    tail = stream.read(65536)
    position = len(tail)
    while (position := tail.rfind(b"OggS", 0, position)) >= 0:
        page = tail[position:]
        if len(page) < 27 or page[4] != 0 or not page[5] & 4 or page[14:18] != serial:
            continue
        segments = page[26]
        if len(page) < 27 + segments or 27 + segments + sum(page[27 : 27 + segments]) != len(page):
            continue
        checksum = struct.unpack_from("<I", page, 22)[0]
        if ogg_crc(page[:22] + bytes(4) + page[26:]) != checksum:
            continue
        (granule,) = struct.unpack_from("<Q", page, 6)
        (pre_skip,) = struct.unpack_from("<H", head, 10)
        if pre_skip <= granule <= 0x7FFFFFFFFFFFFFFF:
            return granule - pre_skip
    return None


def opus_info(stream, info):
    """Validate the OpusHead page (one stream, family 0, 1-2 channels) and read its length."""
    stream.seek(0)
    page = stream.read(27)
    if len(page) != 27 or page[:4] != b"OggS" or page[4] != 0 or page[5] != 2:
        raise ValueError("not-an-ogg-opus-stream")
    lacing = stream.read(page[26])
    if not lacing or len(lacing) != page[26] or lacing[-1] == 255:
        raise ValueError("not-an-ogg-opus-stream")
    head = stream.read(sum(lacing))
    if (
        len(head) != sum(lacing)
        or ogg_crc(page[:22] + bytes(4) + page[26:] + lacing + head) != struct.unpack_from("<I", page, 22)[0]
    ):
        raise ValueError("not-an-ogg-opus-stream")
    if len(lacing) != 1 or len(head) < 19 or head[:8] != b"OpusHead" or head[8] >> 4:
        raise ValueError("not-an-ogg-opus-stream")
    channels, family = head[9], head[18]
    if channels not in (1, 2) or family != 0:
        raise ValueError("unsupported-opus-channels")
    samples = final_samples(stream, head, info)
    if samples is None:
        raise ValueError("opus-length-unknown")
    return OpusInfo(samples, channels, struct.unpack_from("<H", head, 10)[0])


def expected_samples(frames, rate):
    """Source frames as time in 48 kHz samples (exact)."""
    return Fraction(frames * OPUS_RATE, rate)


def check_length(decoded, frames, rate, tolerance=OPUS_FRAME):
    if abs(decoded - expected_samples(frames, rate)) > tolerance:
        raise ValueError("track-length-mismatch")


def describe(value, load, *, recovered=False):
    """Validate one span and its file; load(parts, kind, loader) reads it confined.

    A recovered Opus span (no trustworthy session.json) takes its decoded
    length and rate as its source metadata.
    """
    codec, parts = span_reference(value)
    if codec == "pcm":
        frames, rate, channels = load(parts, "wave", wave_header)
        if any(k in value and value[k] != actual for k, actual in (("sampleRate", rate), ("channels", channels))):
            raise ValueError("track-format-mismatch")
        return SpanInfo(codec, parts, frames, rate, channels, frames, rate)
    opus = load(parts, "opus-track", opus_info)
    if recovered:
        return SpanInfo(codec, parts, opus.samples, OPUS_RATE, opus.channels, opus.samples, OPUS_RATE)
    rate, frames = value.get("sampleRate"), value.get("frames")
    if type(rate) is not int or not 8000 <= rate <= 192000 or type(frames) is not int or not 0 <= frames <= MAX_FRAMES:
        raise ValueError("track-source-metadata-invalid")
    check_length(opus.samples, frames, rate)
    return SpanInfo(codec, parts, frames, rate, opus.channels, opus.samples, OPUS_RATE)


def inspect(directory, value, *, recovered=False):
    """describe() for one span of a session folder path, without a cache."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:

        def load(parts, _kind, loader):
            with open_span(fd, parts) as (stream, info):
                return loader(stream, info)

        return describe(value, load, recovered=recovered)
    finally:
        os.close(fd)


def recovered_opus(directory_fd, limit=64):
    """Sorted `tracks/*.opus` names of a session, for salvage without session.json."""
    try:
        folder = os.open(TRACKS, NOFOLLOW_DIRECTORY, dir_fd=directory_fd)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return []
        raise
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".opus") and not n.startswith("."))
    finally:
        os.close(folder)
    return [f"{TRACKS}/{n}" for n in names[:limit]]


class OpusDecoder:
    """Sequential, seekable 48 kHz S16LE decode of one confined Opus span.

    filesrc reopens the already confined descriptor through /proc/self/fd, so
    the pipeline is seekable without ever resolving the session path again.
    Memory stays bounded: appsink holds at most four decoder buffers and read()
    returns at most what it was asked for. No PCM is written anywhere.
    """

    def __init__(self, path, *, inactivity_seconds=30):
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self.Gst = Gst
        self.inactivity_seconds = inactivity_seconds
        self.fd = open_track(path, "opus")
        self.pipeline = None
        try:
            with os.fdopen(os.dup(self.fd), "rb") as stream:
                self.info = opus_info(stream, os.fstat(self.fd))
            self.channels = self.info.channels
            self.frame_bytes = 2 * self.channels
            self.pending = bytearray()
            self._start()
        except BaseException:
            self.close()
            raise

    def _check_error(self):
        message = self.pipeline.get_bus().pop_filtered(self.Gst.MessageType.ERROR)
        if message:
            raise ValueError(message.parse_error()[0].message)

    def _pull(self):
        Gst = self.Gst
        last = time.monotonic()
        while True:
            self._check_error()
            sample = self.sink.emit("try-pull-sample", Gst.SECOND // 10)
            if sample is None:
                self._check_error()
                if self.sink.get_property("eos"):
                    self.ended = True
                    return
                if time.monotonic() - last > self.inactivity_seconds:
                    raise TimeoutError("Recording source decoder stopped making progress")
                continue
            last = time.monotonic()
            buffer = sample.get_buffer()
            data = buffer.extract_dup(0, buffer.get_size())
            if self.align is not None:
                if buffer.pts == Gst.CLOCK_TIME_NONE:
                    raise ValueError("Recording source decoder lost its position")
                start = (buffer.pts * OPUS_RATE + Gst.SECOND // 2) // Gst.SECOND
                if start + len(data) // self.frame_bytes <= self.align:
                    continue
                if start > self.align:
                    data = bytes((start - self.align) * self.frame_bytes) + data
                else:
                    data = data[(self.align - start) * self.frame_bytes :]
                self.align = None
            self.pending += data
            return

    def read(self, frames):
        """Up to `frames` frames from the current position; fewer only at the end."""
        need = max(0, frames) * self.frame_bytes
        while len(self.pending) < need and not self.ended:
            self._pull()
        data = bytes(self.pending[:need])
        del self.pending[:need]
        self.position += len(data) // self.frame_bytes
        return data

    def seek(self, frame):
        """Sample-exact repositioning; a seek decodes PREROLL frames it discards.

        A flushed libopus keeps the state of the old position and oggdemux
        cannot preroll before the first page, so a seek lands PREROLL early (or
        restarts the decoder near the start) and decodes forward to `frame`.
        """
        if frame == self.position:
            return
        if self.position < frame <= self.position + OPUS_RATE:
            self.read(frame - self.position)
            if self.position == frame:
                return
        self.pending.clear()
        if frame >= self.info.samples:
            self.position, self.align, self.ended = frame, None, True
            return
        Gst = self.Gst
        start = frame - PREROLL
        if start < OPUS_RATE:
            self._start()
        else:
            target = (start * Gst.SECOND + OPUS_RATE - 1) // OPUS_RATE
            if not self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, target):
                raise ValueError("Could not seek recording source")
            self.position, self.align, self.ended = start, start, False
        self.read(frame - self.position)

    def _start(self):
        """A fresh pipeline at frame 0: new decoder state and freshly linked demuxer pads."""
        Gst = self.Gst
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = Gst.parse_launch(
            "filesrc name=file ! oggdemux ! opusdec ! audioconvert dithering=0 noise-shaping=0 ! "
            f"audio/x-raw,format=S16LE,layout=interleaved,rate={OPUS_RATE},channels={self.channels} ! "
            "appsink name=pcm sync=false max-buffers=4 drop=false"
        )
        self.pipeline.get_by_name("file").set_property("location", f"/proc/self/fd/{self.fd}")
        self.sink = self.pipeline.get_by_name("pcm")
        self.pending.clear()
        self.position, self.align, self.ended = 0, None, False
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise ValueError("Could not decode recording source")
        result, _state, _pending = self.pipeline.get_state(10 * Gst.SECOND)
        self._check_error()
        if result == Gst.StateChangeReturn.FAILURE:
            raise ValueError("Could not decode recording source")

    def close(self):
        if self.pipeline is not None:
            self.pipeline.set_state(self.Gst.State.NULL)
            self.pipeline = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def mono_blocks(path):
    """D11.2 input for a compacted span: float32 mono at 48 kHz, <= one second per block."""
    import numpy as np

    decoder = OpusDecoder(path)
    try:
        while data := decoder.read(OPUS_RATE):
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, decoder.channels)
            # Same arithmetic as received Opus input: average in float32, then scale.
            yield samples.mean(axis=1, dtype=np.float32) / np.float32(32768)
    finally:
        decoder.close()
