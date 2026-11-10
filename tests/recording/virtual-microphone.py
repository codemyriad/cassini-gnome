# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the synthetic mic, or independently verify its Pulse monitor."""

import argparse
import json
import math
import os
import signal
import struct
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GLibUnix, Gst  # noqa: E402

RATE = 48000
TONE = 660
WINDOW = RATE // 10
MIN_POWER = 0.001
SINK = "cassini_microphone"
MONITOR = f"{SINK}.monitor"


def produce():
    pipeline = Gst.parse_launch(
        f"audiotestsrc is-live=true freq={TONE} volume=0.2 ! audioconvert ! audioresample ! "
        f"audio/x-raw,rate={RATE},channels=1 ! pulsesink device={SINK} sync=false"
    )
    loop = GLib.MainLoop()
    errors = []

    def on_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            errors.append(error)
            print(f"Virtual microphone producer: {error}; {debug}", file=sys.stderr, flush=True)
            loop.quit()
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == pipeline:
            old, new, pending = message.parse_state_changed()
            print(
                f"Virtual microphone producer: {old.value_nick} -> {new.value_nick} (pending {pending.value_nick})",
                file=sys.stderr,
                flush=True,
            )

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)

    def stop():
        loop.quit()
        return GLib.SOURCE_CONTINUE

    signal_watch = GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not start the virtual microphone producer")
        loop.run()
        if errors:
            raise RuntimeError("Virtual microphone producer failed") from errors[0]
    finally:
        pipeline.set_state(Gst.State.NULL)
        bus.remove_signal_watch()
        GLib.source_remove(signal_watch)


def tone_power(pcm):
    samples = [sample / 32768 for (sample,) in struct.iter_unpack("<h", pcm)]
    omega = math.tau * TONE / RATE
    real = sum(sample * math.cos(omega * i) for i, sample in enumerate(samples))
    imag = sum(sample * math.sin(omega * i) for i, sample in enumerate(samples))
    return (real * real + imag * imag) / (WINDOW * WINDOW)


def probe():
    pipeline = Gst.parse_launch(
        f"pulsesrc device={MONITOR} client-name=cassini-mic-readiness buffer-time=100000 latency-time=10000 ! "
        f"audio/x-raw,format=S16LE,rate={RATE},channels=1 ! appsink name=pcm sync=false"
    )
    sink = pipeline.get_by_name("pcm")
    bus = pipeline.get_bus()
    deadline = time.monotonic_ns() + 10 * Gst.SECOND
    pending = bytearray()
    peak, frames = 0.0, 0
    try:
        pipeline.set_state(Gst.State.PLAYING)
        while (remaining := deadline - time.monotonic_ns()) > 0:
            sample = sink.emit("try-pull-sample", remaining)
            if error := bus.pop_filtered(Gst.MessageType.ERROR):
                detail, debug = error.parse_error()
                raise RuntimeError(f"Cannot read {MONITOR}: {detail}; {debug}")
            if sample is None:
                break
            buffer = sample.get_buffer()
            frames += buffer.get_size() // 2
            pending.extend(buffer.extract_dup(0, buffer.get_size()))
            while len(pending) >= 2 * WINDOW:
                power = tone_power(pending[: 2 * WINDOW])
                del pending[: 2 * WINDOW]
                peak = max(peak, power)
                if power > MIN_POWER:
                    return {"monitor": MONITOR, "toneHz": TONE, "power": power}
        raise RuntimeError(
            f"{MONITOR} did not deliver the {TONE} Hz fixture tone ({frames} frames, peak power {peak:.6g})"
        )
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("produce", "probe"))
    args = parser.parse_args()
    if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or not os.environ.get("PULSE_SERVER", "").startswith(
        "unix:/tmp/cassini-audio-"
    ):
        parser.error("Use the private recording harness")
    Gst.init(None)
    if args.mode == "produce":
        produce()
    else:
        print(json.dumps(probe()))
