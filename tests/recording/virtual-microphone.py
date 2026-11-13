# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a synthetic Pulse fixture tone, or independently verify its monitor."""

import argparse
import json
import math
import os
import signal
import struct
import sys
import time

RATE = 48000
TONE = 660
WINDOW = RATE // 10
MIN_POWER = 0.001
SINK = "cassini_microphone"
SINKS = ("cassini_microphone", "cassini_desktop")
MONITOR = f"{SINK}.monitor"


def _gst():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, GLibUnix, Gst

    Gst.init(None)
    return GLib, GLibUnix, Gst


def produce_pipeline(*, sink=SINK, hz=TONE, channels=1):
    return (
        f"audiotestsrc is-live=true freq={hz} volume=0.2 ! audioconvert ! audioresample ! "
        f"audio/x-raw,rate={RATE},channels={channels} ! pulsesink device={sink} sync=false provide-clock=false"
    )


def probe_pipeline(*, sink=SINK):
    monitor = f"{sink}.monitor"
    return (
        f"pulsesrc device={monitor} provide-clock=false client-name=cassini-mic-readiness "
        f"buffer-time=100000 latency-time=10000 ! audioconvert ! audioresample ! "
        f"audio/x-raw,format=S16LE,rate={RATE},channels=1 ! appsink name=pcm sync=false"
    )


def produce(*, sink=SINK, hz=TONE, channels=1):
    GLib, GLibUnix, Gst = _gst()
    pipeline = Gst.parse_launch(produce_pipeline(sink=sink, hz=hz, channels=channels))
    loop = GLib.MainLoop()
    errors = []

    def on_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            errors.append(error)
            print(f"Virtual {sink} producer: {error}; {debug}", file=sys.stderr, flush=True)
            loop.quit()
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == pipeline:
            old, new, pending = message.parse_state_changed()
            print(
                f"Virtual {sink} producer: {old.value_nick} -> {new.value_nick} (pending {pending.value_nick})",
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
            raise RuntimeError(f"Could not start the virtual {sink} producer")
        loop.run()
        if errors:
            raise RuntimeError(f"Virtual {sink} producer failed") from errors[0]
    finally:
        pipeline.set_state(Gst.State.NULL)
        bus.remove_signal_watch()
        GLib.source_remove(signal_watch)


def tone_power(pcm, *, hz=TONE):
    samples = [sample / 32768 for (sample,) in struct.iter_unpack("<h", pcm)]
    omega = math.tau * hz / RATE
    real = sum(sample * math.cos(omega * i) for i, sample in enumerate(samples))
    imag = sum(sample * math.sin(omega * i) for i, sample in enumerate(samples))
    return (real * real + imag * imag) / (WINDOW * WINDOW)


def probe(*, sink=SINK, hz=TONE):
    _GLib, _GLibUnix, Gst = _gst()
    monitor = f"{sink}.monitor"
    pipeline = Gst.parse_launch(probe_pipeline(sink=sink))
    pcm_sink = pipeline.get_by_name("pcm")
    bus = pipeline.get_bus()
    deadline = time.monotonic_ns() + 10 * Gst.SECOND
    pending = bytearray()
    peak, frames = 0.0, 0
    try:
        pipeline.set_state(Gst.State.PLAYING)
        while (remaining := deadline - time.monotonic_ns()) > 0:
            sample = pcm_sink.emit("try-pull-sample", remaining)
            if error := bus.pop_filtered(Gst.MessageType.ERROR):
                detail, debug = error.parse_error()
                raise RuntimeError(f"Cannot read {monitor}: {detail}; {debug}")
            if sample is None:
                break
            buffer = sample.get_buffer()
            frames += buffer.get_size() // 2
            pending.extend(buffer.extract_dup(0, buffer.get_size()))
            while len(pending) >= 2 * WINDOW:
                power = tone_power(pending[: 2 * WINDOW], hz=hz)
                del pending[: 2 * WINDOW]
                peak = max(peak, power)
                if power > MIN_POWER:
                    return {"monitor": monitor, "toneHz": hz, "power": power}
        raise RuntimeError(
            f"{monitor} did not deliver the {hz} Hz fixture tone ({frames} frames, peak power {peak:.6g})"
        )
    finally:
        pipeline.set_state(Gst.State.NULL)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("produce", "probe"))
    parser.add_argument("--sink", choices=SINKS, default=SINK)
    parser.add_argument("--hz", type=int, default=TONE)
    parser.add_argument("--channels", type=int, choices=(1, 2), default=1)
    args = parser.parse_args(argv)
    if not (100 <= args.hz <= 20000):
        parser.error("--hz must be between 100 and 20000")
    return parser, args


if __name__ == "__main__":
    parser, args = parse_args()
    if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or not os.environ.get("PULSE_SERVER", "").startswith(
        "unix:/tmp/cassini-audio-"
    ):
        parser.error("Use the private recording harness")
    if args.mode == "produce":
        produce(sink=args.sink, hz=args.hz, channels=args.channels)
    else:
        print(json.dumps(probe(sink=args.sink, hz=args.hz)))
