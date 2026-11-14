# SPDX-License-Identifier: GPL-3.0-or-later
"""L-25 / W4.3: decode a decided plain-audio import (MP3, M4A, WAV) into Ogg Opus.

The original file is never modified; the Library keeps an Opus copy so the
meeting is an ordinary portable meeting (Received / "Imported" provenance).
Runs on the import worker, never on the GTK main thread.
"""

import contextlib
import os
import time

from .export_audio import check_cancel

# Speech-oriented VBR target; the decoded stream keeps 1 or 2 channels.
OPUS_BITRATE_BPS = 64000
# A local decode that produces no new position for this long is stuck.
STALL_SECONDS = 30
BUS_POLL_NS = 100_000_000


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

    Raises ValueError when the input carries no decodable audio, and
    CancelledError (via check_cancel) when ``cancel`` is set. ``output`` must
    not exist; it is removed again on failure.
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    if missing_elements():
        raise ValueError("Audio decoding is not available on this computer")
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
        completed = True
    finally:
        pipeline.set_state(Gst.State.NULL)
        if not completed:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(output)
