#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise actual PulseAudio sources, the Opus encoder, and channel isolation."""

import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

if not os.environ.get("CASSINI_ISOLATED_AUDIO") or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh to isolate desktop and audio")
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cassini_viewer.recorder import Recorder  # noqa: E402

Gst.init(None)
artifacts = Path(os.environ.get("CASSINI_TEST_ARTIFACTS", str(Path(__file__).parent / "artifacts" / "native")))
artifacts.mkdir(parents=True, exist_ok=True)
context = GLib.MainContext.default()


def spin(seconds, until=lambda: False):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not until():
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


producers = []
recorder = Recorder()
errors, finished = [], []
recorder.connect("error", lambda _r, message: errors.append(message))
recorder.connect("finished", lambda _r, path: finished.append(path))
try:
    for device, frequency in (("cassini_microphone", 440), ("cassini_desktop", 880)):
        pipeline = Gst.parse_launch(
            f"audiotestsrc is-live=true freq={frequency} volume=0.2 ! audioconvert ! audioresample ! pulsesink device={device}"
        )
        pipeline.set_state(Gst.State.PLAYING)
        producers.append(pipeline)
    spin(0.5)
    destination = artifacts / "native.opus"
    recorder.start(destination)
    spin(2.5)
    assert not errors, errors
    recorder.stop()
    spin(12, lambda: bool(finished or errors))
    assert not errors, errors
    assert finished == [str(destination)]
    decoded = artifacts / "native.wav"
    pipeline = Gst.parse_launch(
        "filesrc name=input ! oggdemux ! opusdec ! audioconvert ! audio/x-raw,format=S16LE ! wavenc ! filesink name=output"
    )
    pipeline.get_by_name("input").set_property("location", str(destination))
    pipeline.get_by_name("output").set_property("location", str(decoded))
    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = pipeline.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        assert message and message.type == Gst.MessageType.EOS
    finally:
        pipeline.set_state(Gst.State.NULL)
    with wave.open(str(decoded)) as audio:
        assert audio.getnchannels() == 2
        rate = audio.getframerate()
        assert audio.getnframes() >= rate * 2
        samples = struct.unpack(f"<{audio.getnframes() * 2}h", audio.readframes(audio.getnframes()))

    def tone(channel, frequency):
        data = samples[rate * 2 + channel : rate * 4 : 2]
        real = sum(v * math.cos(2 * math.pi * frequency * n / rate) for n, v in enumerate(data))
        imag = sum(v * math.sin(2 * math.pi * frequency * n / rate) for n, v in enumerate(data))
        return 2 * math.hypot(real, imag) / len(data) / 32768

    amplitudes = {"left440": tone(0, 440), "left880": tone(0, 880), "right440": tone(1, 440), "right880": tone(1, 880)}
    assert amplitudes["left440"] > 0.08 and amplitudes["right880"] > 0.08, amplitudes
    assert amplitudes["left880"] < 0.01 and amplitudes["right440"] < 0.01, amplitudes
    (artifacts / "results.json").write_text(json.dumps({"passed": True, "amplitudes": amplitudes}, indent=2) + "\n")
    print(
        "PASS actual PulseAudio capture → Opus → decoded audio: microphone left, desktop right, no cross-channel bleed"
    )
finally:
    recorder._cleanup()
    for pipeline in producers:
        pipeline.set_state(Gst.State.NULL)
