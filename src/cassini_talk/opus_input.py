# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, back-pressured Opus decoding inside the inference child only."""

import time
from pathlib import Path

import numpy as np


def pcm_blocks(path, *, inactivity_seconds=30):
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    pipeline = Gst.parse_launch(
        "uridecodebin name=source ! audioconvert dithering=0 noise-shaping=0 ! "
        "audio/x-raw,format=S16LE,rate=48000,channels=(int)[1,2] ! "
        "appsink name=pcm max-buffers=2 drop=false sync=false"
    )
    pipeline.get_by_name("source").set_property("uri", Path(path).resolve().as_uri())
    sink, bus = pipeline.get_by_name("pcm"), pipeline.get_bus()
    pending, size = [], 0
    last = time.monotonic()
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not decode received audio")
        while True:
            message = bus.pop_filtered(Gst.MessageType.ERROR)
            if message:
                raise RuntimeError(message.parse_error()[0].message)
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                if sink.get_property("eos"):
                    break
                if time.monotonic() - last > inactivity_seconds:
                    raise RuntimeError("Received audio decoder stopped making progress")
                continue
            last = time.monotonic()
            channels = sample.get_caps().get_structure(0).get_value("channels")
            buffer = sample.get_buffer()
            if channels not in (1, 2) or buffer.get_size() > 48000 * 2 * 2 * 4:
                raise ValueError("Received audio decoder exceeded its block limit")
            raw = buffer.extract_dup(0, buffer.get_size())
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels)
            # D11.2: preserve stereo until averaging in float32 before FIR.
            mono = samples.mean(axis=1, dtype=np.float32) / np.float32(32768)
            while len(mono):
                take = min(48000 - size, len(mono))
                pending.append(mono[:take])
                mono = mono[take:]
                size += take
                if size == 48000:
                    yield np.concatenate(pending)
                    pending, size = [], 0
        if pending:
            yield np.concatenate(pending)
        message = bus.pop_filtered(Gst.MessageType.ERROR)
        if message:
            raise RuntimeError(message.parse_error()[0].message)
    finally:
        pipeline.set_state(Gst.State.NULL)
