#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise actual PulseAudio sources, the Opus encoder, and channel isolation."""

import faulthandler
import json
import math
import os
import struct
import sys
import traceback
import wave
from pathlib import Path

if not os.environ.get("CASSINI_ISOLATED_AUDIO") or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh to isolate desktop and audio")
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from gst_evidence import GstEvidence  # noqa: E402
from readiness import PCMProgress, wait_until  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cassini_viewer.recorder import Recorder  # noqa: E402

Gst.init(None)
artifacts = Path(os.environ.get("CASSINI_TEST_ARTIFACTS", str(Path(__file__).parent / "artifacts" / "native")))
artifacts.mkdir(parents=True, exist_ok=True)
# Also bound C-level state changes and retain traces in the failure artifact.
fault_log = (artifacts / "threads.log").open("w")
faulthandler.dump_traceback_later(60, file=fault_log, exit=True)
producers, monitors, observations = [], [], []
capture_progress = None
evidence = {"passed": False}
recorder = Recorder()
trace = GstEvidence(artifacts)
trace.hook_messages(Recorder)
trace.hook_failure(Recorder)
errors, finished = [], []
recorder.connect("error", lambda _r, message: errors.append(message))
recorder.connect("finished", lambda _r, path: finished.append(path))


def diagnostics():
    return dict(
        errors=errors,
        gst=trace.snapshot(),
        finished=finished,
        producers=[p.snapshot() for p in observations],
        capture=capture_progress.snapshot() if capture_progress else None,
    )


def checkpoint(stage):
    trace.checkpoint(stage, recorder.pipeline)
    evidence["stage"] = stage
    evidence["readiness"] = diagnostics()
    (artifacts / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")


def sources_ready():
    for pipeline in producers + monitors:
        message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if message:
            error, debug = message.parse_error()
            raise AssertionError(f"Virtual producer/monitor failed: {error.message}; {debug}")
    return all(p.reached(1, audible=True) for p in observations)


try:
    checkpoint("starting-producers")
    for device, frequency in (("cassini_microphone", 440), ("cassini_desktop", 880)):
        pipeline = Gst.parse_launch(
            f"audiotestsrc is-live=true freq={frequency} volume=0.2 ! audioconvert ! audioresample ! pulsesink device={device}"
        )
        pipeline.set_state(Gst.State.PLAYING)
        producers.append(pipeline)
        # Observe the actual private PulseAudio monitor, not just queued producer
        # buffers or a pipeline state transition. A delayed/silent producer cannot
        # satisfy one second of continuously nonzero PCM here.
        monitor = Gst.parse_launch(
            f"pulsesrc device={device}.monitor ! audioconvert ! audioresample ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=1 ! fakesink name=observed sync=false"
        )
        observations.append(PCMProgress(monitor.get_by_name("observed").get_static_pad("sink"), observe_audio=True))
        monitors.append(monitor)
        monitor.set_state(Gst.State.PLAYING)
    wait_until(sources_ready, "both virtual producers deliver audible PCM", diagnostics=diagnostics)
    for observation in observations:
        observation.close()
    for monitor in monitors:
        monitor.set_state(Gst.State.NULL)
    destination = artifacts / "native.opus"
    checkpoint("starting-capture")
    recorder.start(destination)
    capture_progress = PCMProgress(recorder.pipeline.get_by_name("capture").get_static_pad("src"))

    def captured_enough():
        assert not errors, errors
        trace.healthy("capturing")
        return capture_progress.reached(2.5)

    wait_until(captured_enough, "recorder receives 2.5 seconds of complete PCM", diagnostics=diagnostics)
    assert not errors, errors
    capture_progress.close()
    checkpoint("finalizing")
    recorder.stop()
    wait_until(lambda: bool(finished or errors), "recorder finalizes Opus", diagnostics=diagnostics)
    assert not errors, errors
    assert finished == [str(destination)]
    checkpoint("decoding")
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
        evidence["decoded"] = dict(frames=audio.getnframes(), rate=rate, channels=audio.getnchannels())
        assert audio.getnframes() >= rate * 2
        samples = struct.unpack(f"<{audio.getnframes() * 2}h", audio.readframes(audio.getnframes()))

    def tone(channel, frequency):
        data = samples[rate * 2 + channel : rate * 4 : 2]
        real = sum(v * math.cos(2 * math.pi * frequency * n / rate) for n, v in enumerate(data))
        imag = sum(v * math.sin(2 * math.pi * frequency * n / rate) for n, v in enumerate(data))
        return 2 * math.hypot(real, imag) / len(data) / 32768

    amplitudes = {"left440": tone(0, 440), "left880": tone(0, 880), "right440": tone(1, 440), "right880": tone(1, 880)}
    evidence["amplitudes"] = amplitudes
    assert amplitudes["left440"] > 0.08 and amplitudes["right880"] > 0.08, amplitudes
    assert amplitudes["left880"] < 0.01 and amplitudes["right440"] < 0.01, amplitudes
    evidence["passed"] = True
    print(
        "PASS actual PulseAudio capture → Opus → decoded audio: microphone left, desktop right, no cross-channel bleed"
    )
except BaseException:
    evidence["failure"] = traceback.format_exc()
    raise
finally:
    assertions_passed = evidence["passed"]
    evidence["passed"] = False
    checkpoint("cleanup")
    cleanup_ok = False
    try:
        for observation in observations:
            observation.close()
        if capture_progress:
            capture_progress.close()
        recorder._cleanup()
        for pipeline in producers + monitors:
            pipeline.set_state(Gst.State.NULL)
        cleanup_ok = True
    except BaseException:
        evidence["cleanupFailure"] = traceback.format_exc()
        raise
    finally:
        evidence["passed"] = assertions_passed and cleanup_ok
        checkpoint("complete" if evidence["passed"] else "failed")
        faulthandler.cancel_dump_traceback_later()
        fault_log.close()
        trace.close()
