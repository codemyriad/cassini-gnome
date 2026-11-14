# SPDX-License-Identifier: GPL-3.0-or-later
"""L-25 / W4.3: decode a decided plain-audio import (MP3, M4A, WAV) into Ogg Opus.

The original file is never modified; the Library keeps an Opus copy so the
meeting is an ordinary portable meeting (Received / "Imported" provenance).
Runs on the import worker, never on the GTK main thread.
"""

import contextlib
import os
import struct
import time
from pathlib import PurePath

from .export_audio import check_cancel

# Speech-oriented VBR target; the decoded stream keeps 1 or 2 channels.
OPUS_BITRATE_BPS = 64000
# A local decode that produces no new position for this long is stuck.
STALL_SECONDS = 30
BUS_POLL_NS = 100_000_000
INCOMPLETE = "This audio file is incomplete"
# RIFF/RF64 size fields set to this value mean "unknown length" (streamed WAV).
WAV_UNKNOWN_SIZE = 0xFFFFFFFF
# How far past the ID3v2 tag the first MPEG frame may start (zero padding).
MP3_SYNC_WINDOW = 64 * 1024


def _read_at(stream, offset, count):
    stream.seek(offset)
    return stream.read(count)


def _check_wav(stream, size):
    """RIFF/RF64: every chunk up to and including ``data`` must fit in the file."""
    head = _read_at(stream, 0, 12)
    if head[:4] not in (b"RIFF", b"RF64"):
        return  # Not a RIFF file: the decoder decides.
    if len(head) < 12:
        raise ValueError(INCOMPLETE)
    if head[8:12] != b"WAVE":
        return
    rf64 = head[:4] == b"RF64"
    riff_size = struct.unpack("<I", head[4:8])[0]
    data_size64 = None
    offset = 12
    while True:
        header = _read_at(stream, offset, 8)
        if len(header) < 8:
            # The header ended before any audio data: the file was cut short.
            raise ValueError(INCOMPLETE)
        chunk, length = header[:4], struct.unpack("<I", header[4:])[0]
        if rf64 and chunk == b"ds64":
            body = _read_at(stream, offset + 8, 16)
            if len(body) < 16:
                raise ValueError(INCOMPLETE)
            riff_size, data_size64 = struct.unpack("<QQ", body)
        if chunk == b"data":
            if rf64 and length == WAV_UNKNOWN_SIZE:
                if data_size64 is None:
                    raise ValueError(INCOMPLETE)
                length = data_size64
            elif length in (0, WAV_UNKNOWN_SIZE):
                return  # Streamed WAV: length unknown, audio runs to end of file.
            if offset + 8 + length > size:
                raise ValueError(INCOMPLETE)
            break
        # Chunks are word aligned; a missing pad byte only matters mid-file.
        offset += 8 + length + (length & 1)
        if offset > size:
            raise ValueError(INCOMPLETE)
    # A declared RIFF length beyond the file also means lost bytes; one missing
    # final pad byte is a common writer quirk and tolerated.
    if riff_size not in (0, WAV_UNKNOWN_SIZE) and riff_size + 8 > size + 1:
        raise ValueError(INCOMPLETE)


def _check_mp4(stream, size):
    """ISO BMFF (M4A): every top-level box must fit, and a ``moov`` must be present."""
    offset, boxes = 0, set()
    while offset < size:
        header = _read_at(stream, offset, 16)
        if len(header) < 8:
            raise ValueError(INCOMPLETE)
        length, kind = struct.unpack(">I4s", header[:8])
        head_bytes = 8
        if length == 1:
            if len(header) < 16:
                raise ValueError(INCOMPLETE)
            length, head_bytes = struct.unpack(">Q", header[8:16])[0], 16
        elif length == 0:
            length = size - offset  # The last box runs to the end of the file.
        if length < head_bytes:
            raise ValueError("This audio file is damaged")
        if offset + length > size:
            raise ValueError(INCOMPLETE)
        boxes.add(kind)
        offset += length
    if b"moov" not in boxes:
        raise ValueError(INCOMPLETE)


def _check_mp3(stream, size):
    """MP3: the ID3v2 tag and any Xing/Info/VBRI stream length must fit in the file."""
    start = 0
    head = _read_at(stream, 0, 10)
    if len(head) == 10 and head[:3] == b"ID3":
        tag = 0
        for byte in head[6:10]:
            tag = (tag << 7) | (byte & 0x7F)
        start = 10 + tag + (10 if head[5] & 0x10 else 0)
        if start > size:
            raise ValueError(INCOMPLETE)
    window = _read_at(stream, start, MP3_SYNC_WINDOW)
    frame = next(
        (i for i in range(len(window) - 3) if window[i] == 0xFF and window[i + 1] & 0xE0 == 0xE0),
        None,
    )
    if frame is None:
        return  # No MPEG frame found: the decoder reports the damage.
    b1, b2, b3 = window[frame + 1], window[frame + 2], window[frame + 3]
    version, layer_bits = (b1 >> 3) & 3, (b1 >> 1) & 3
    if version == 1 or layer_bits == 0 or (b2 >> 4) in (0, 15) or (b2 >> 2) & 3 == 3:
        return  # Reserved/free-format header: nothing reliable to compare.
    mpeg1, mono = version == 3, (b3 >> 6) == 3
    # The Xing/Info tag follows the side information of the first Layer III frame.
    side = (32 if not mono else 17) if mpeg1 else (17 if not mono else 9)
    begin = start + frame
    body = _read_at(stream, begin + 4 + side, 16)
    declared = None
    if body[:4] in (b"Xing", b"Info") and len(body) >= 8:
        flags = struct.unpack(">I", body[4:8])[0]
        if flags & 2:
            field = 8 + (4 if flags & 1 else 0)
            more = _read_at(stream, begin + 4 + side + field, 4)
            if len(more) < 4:
                raise ValueError(INCOMPLETE)
            declared = struct.unpack(">I", more)[0]
    else:
        vbri = _read_at(stream, begin + 36, 14)
        if vbri[:4] == b"VBRI" and len(vbri) == 14:
            declared = struct.unpack(">I", vbri[10:14])[0]
    if declared and begin + declared > size:
        raise ValueError(INCOMPLETE)


CONTAINER_CHECKS = {".wav": _check_wav, ".m4a": _check_mp4, ".mp3": _check_mp3}


def check_complete(source):
    """Raise ValueError when ``source``'s container declares more bytes than it holds.

    A decoder stops cleanly at end of file, so a truncated WAV/M4A/MP3 would
    otherwise import as a shorter meeting with no warning. The checks rely on
    the container's own length fields; formats without them pass.
    """
    check = CONTAINER_CHECKS.get(PurePath(os.fspath(source)).suffix.lower())
    if check is None:
        return
    with open(source, "rb") as stream:
        check(stream, os.fstat(stream.fileno()).st_size)


def _fsync_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def missing_elements():
    """GStreamer elements this decode path needs that are not installed."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return [
        name
        for name in ("decodebin", "audioconvert", "audioresample", "opusenc", "oggmux")
        if not Gst.ElementFactory.find(name)
    ]


def decode_to_opus(source, output, *, cancel=None, clock=time.monotonic):
    """Decode ``source`` (a regular file path) into a new Ogg Opus file at ``output``.

    Raises ValueError when the input carries no decodable audio or its
    container is truncated, and CancelledError (via check_cancel) when
    ``cancel`` is set. ``output`` must not exist; it is removed again on
    failure. On success the Opus file has been fsynced (filesink does not).
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    if missing_elements():
        raise ValueError("Audio decoding is not available on this computer")
    check_complete(source)
    pipeline = Gst.parse_launch(
        "filesrc name=input ! decodebin name=decode "
        "audioconvert name=convert dithering=0 noise-shaping=0 ! audioresample ! "
        "audio/x-raw,rate=48000,channels=(int)[1,2] ! "
        f"opusenc bitrate={OPUS_BITRATE_BPS} bitrate-type=vbr audio-type=generic ! "
        "oggmux ! filesink name=output"
    )
    pipeline.get_by_name("input").set_property("location", os.fspath(source))
    pipeline.get_by_name("output").set_property("location", os.fspath(output))
    convert = pipeline.get_by_name("convert")
    linked = []

    def pad_added(_decode, pad):
        # Only the first audio stream is linked; any other stream stays unlinked.
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        sink = convert.get_static_pad("sink")
        if name.startswith("audio/") and not sink.is_linked() and pad.link(sink) == Gst.PadLinkReturn.OK:
            linked.append(name)

    pipeline.get_by_name("decode").connect("pad-added", pad_added)
    bus = pipeline.get_bus()
    completed = False
    try:
        try:
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise ValueError("This audio file could not be decoded")
            last_position, last_progress = -1, clock()
            while True:
                check_cancel(cancel)
                message = bus.timed_pop_filtered(BUS_POLL_NS, Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if message is not None:
                    if message.type == Gst.MessageType.ERROR:
                        raise ValueError(message.parse_error()[0].message)
                    break
                ok, position = pipeline.query_position(Gst.Format.TIME)
                if ok and position != last_position:
                    last_position, last_progress = position, clock()
                elif clock() - last_progress > STALL_SECONDS:
                    raise ValueError("The audio decoder stopped making progress")
            if not linked:
                raise ValueError("This file has no audio")
        finally:
            # NULL closes filesink's descriptor before the file is synced.
            pipeline.set_state(Gst.State.NULL)
        _fsync_file(output)
        completed = True
    finally:
        if not completed:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(output)
