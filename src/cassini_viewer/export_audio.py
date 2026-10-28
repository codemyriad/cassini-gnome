# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded PCM sources, timeline mix and a 0.95 lookahead limiter into Opus."""

import array
import math
import os
import time
from collections import deque
from concurrent.futures import CancelledError
from itertools import zip_longest

RATE = 48000


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise CancelledError()


def track_pcm(track, cancel=None):
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from .folder_player import PCMSource

    Gst.init(None)
    source = PCMSource(track)
    pipeline = Gst.parse_launch(
        "audioconvert dithering=0 noise-shaping=0 ! audioresample ! "
        "audio/x-raw,format=F32LE,layout=interleaved,channels=1,rate=48000 ! "
        "appsink name=pcm sync=false max-buffers=2 drop=false"
    )
    first = next(e for e in _elements(pipeline) if e.get_factory().get_name() == "audioconvert")
    pipeline.add(source.src)
    if not source.src.link(first):
        raise RuntimeError("Could not link recording source for export")
    sink = pipeline.get_by_name("pcm")
    remaining = array.array("f")
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not read recording for export")
        last_progress = time.monotonic()
        while True:
            check_cancel(cancel)
            error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if error:
                raise ValueError(error.parse_error()[0].message)
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                # PCMSource posts its read error before EOS. EOS can wake a
                # blocked pull after the check above, so it must not turn that
                # failure into a successful (possibly empty) exported track.
                error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
                if error:
                    raise ValueError(error.parse_error()[0].message)
                if sink.get_property("eos"):
                    break
                if time.monotonic() - last_progress > 30:
                    raise TimeoutError("Recording source stopped making progress")
                continue
            last_progress = time.monotonic()
            buffer = sample.get_buffer()
            block = array.array("f", buffer.extract_dup(0, buffer.get_size()))
            if os.sys.byteorder != "little":
                block.byteswap()
            remaining.extend(block)
            while len(remaining) >= RATE:
                yield remaining[:RATE]
                del remaining[:RATE]
        if remaining:
            yield remaining
    finally:
        pipeline.set_state(Gst.State.NULL)
        source.close()


def _elements(pipeline):
    from gi.repository import Gst

    iterator = pipeline.iterate_elements()
    while True:
        result, element = iterator.next()
        if result != Gst.IteratorResult.OK:
            return
        yield element


def mixed_pcm(tracks, cancel=None):
    """Each source exposes the same meeting timeline, including known offsets."""
    streams = [track_pcm(track, cancel) for track in tracks if track.frames]
    if not streams:
        raise ValueError("Meeting has no complete audio frames")
    try:
        while True:
            check_cancel(cancel)
            chunks = [next(stream, ()) for stream in streams]
            if not any(chunks):
                return
            yield array.array("f", (sum(values) for values in zip_longest(*chunks, fillvalue=0.0)))
    finally:
        for stream in streams:
            stream.close()


def limited_pcm(chunks, *, limit=0.95, lookahead=240, release=2400):
    """Five-ms lookahead and 50-ms release; preserve duration and quiet levels."""
    pending, peaks = deque(), deque()
    index, gain = -1, 1.0

    def push(sample):
        nonlocal index, gain
        index += 1
        pending.append(sample)
        while peaks and peaks[-1][1] <= abs(sample):
            peaks.pop()
        peaks.append((index, abs(sample)))
        oldest = index - lookahead
        while peaks and peaks[0][0] < oldest:
            peaks.popleft()
        if len(pending) <= lookahead:
            return None
        needed = min(1.0, limit / peaks[0][1]) if peaks[0][1] else 1.0
        gain = min(needed, gain + (1 - gain) / release)
        return pending.popleft() * gain

    for chunk in chunks:
        output = array.array("f")
        for sample in chunk:
            if not math.isfinite(sample):
                raise ValueError("Recording contains non-finite PCM")
            value = push(sample)
            if value is not None:
                output.append(value)
        if output:
            yield output
    tail = array.array("f")
    for _ in range(lookahead if pending else 0):
        value = push(0.0)
        if value is not None:
            tail.append(value)
    if tail:
        yield tail


def encode_pcm(tracks, output, choice, cancel=None):
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    bandwidth = {4000: "narrowband", 6000: "mediumband", 8000: "wideband", 12000: "superwideband", 20000: "fullband"}[
        choice["cutoffHz"]
    ]
    pipeline = Gst.parse_launch(
        "appsrc name=pcm format=time block=true max-bytes=192000 "
        "caps=audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=1 ! "
        "audioconvert dithering=0 noise-shaping=0 ! "
        f"opusenc bitrate={choice['bitrateBps']} bitrate-type=vbr complexity=10 audio-type=generic bandwidth={bandwidth} ! "
        "oggmux ! filesink name=output"
    )
    source = pipeline.get_by_name("pcm")
    pipeline.get_by_name("output").set_property("location", str(output))
    cursor = 0
    stream = limited_pcm(mixed_pcm(tracks, cancel))
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not start the Opus encoder")
        for block in stream:
            check_cancel(cancel)
            error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if error:
                raise ValueError(error.parse_error()[0].message)
            if os.sys.byteorder != "little":
                block.byteswap()
            data = block.tobytes()
            buffer = Gst.Buffer.new_allocate(None, len(data), None)
            buffer.fill(0, data)
            buffer.pts = cursor * Gst.SECOND // RATE
            buffer.duration = len(block) * Gst.SECOND // RATE
            cursor += len(block)
            if source.emit("push-buffer", buffer) != Gst.FlowReturn.OK:
                raise RuntimeError("Opus encoder refused PCM")
        source.emit("end-of-stream")
        deadline = time.monotonic() + 30
        while True:
            check_cancel(cancel)
            message = pipeline.get_bus().timed_pop_filtered(Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if message:
                if message.type == Gst.MessageType.ERROR:
                    raise ValueError(message.parse_error()[0].message)
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Opus encoder did not finalize")
    finally:
        pipeline.set_state(Gst.State.NULL)
        stream.close()
