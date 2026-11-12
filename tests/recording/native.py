#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise actual PulseAudio sources, the Opus encoder, and channel isolation."""

import faulthandler
import json
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
from signal_power import stereo_tones  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from cassini_talk.library_publication import capture_parent  # noqa: E402
from cassini_talk.native_recorder import NativeRecorder  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402

Gst.init(None)
artifacts = Path(os.environ.get("CASSINI_TEST_ARTIFACTS", str(Path(__file__).parent / "artifacts" / "native")))
artifacts.mkdir(parents=True, exist_ok=True)
# Also bound C-level state changes and retain traces in the failure artifact.
fault_log = (artifacts / "threads.log").open("w")
faulthandler.dump_traceback_later(60, file=fault_log, exit=True)
producers, monitors, observations = [], [], []
evidence = {"passed": False}
recorder = NativeRecorder()
trace = GstEvidence(artifacts)
trace.hook_messages(NativeRecorder)
trace.hook_appends(RecordingSession)
errors, finished = [], []
recorder.connect("error", lambda _r, message: errors.append(message))
recorder.connect("finished", lambda _r, path: finished.append(path))


def capture_snapshot():
    if recorder.session is None:
        return None
    with recorder._lock:
        return {
            kind: dict(frames=writer.frames, sampleRate=48000, channels=1)
            for kind, writer in recorder.session.writers.items()
        }


def diagnostics():
    return dict(
        errors=errors,
        gst=trace.snapshot(),
        finished=finished,
        producers=[p.snapshot() for p in observations],
        capture=capture_snapshot(),
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
    library = artifacts / "library"
    sessions_dir = capture_parent(library)
    checkpoint("starting-capture")
    recorder.start(sessions_dir, observe_output=False)

    def captured_enough():
        assert not errors, errors
        trace.healthy("capturing")
        if recorder.session is None:
            return False
        with recorder._lock:
            return all(
                recorder.session.writers.get(kind) is not None
                and recorder.session.writers[kind].frames >= int(48000 * 2.5)
                for kind in ("microphone", "system")
            )

    wait_until(captured_enough, "recorder receives 2.5 seconds of complete PCM on both sides", diagnostics=diagnostics)
    assert not errors, errors
    checkpoint("finalizing")
    recorder.stop()
    wait_until(lambda: bool(finished or errors), "recorder finalizes session and Opus", diagnostics=diagnostics)
    assert not errors, errors
    session_dir = recorder.session.directory
    assert finished == [str(session_dir)]
    assert recorder.publication is not None and recorder.publication_error is None
    opus_files = list(library.glob("*.opus"))
    assert len(opus_files) == 1, opus_files
    destination = opus_files[0]

    checkpoint("decoding")
    tracks = {track["kind"]: session_dir / track["file"] for track in recorder.session.manifest["tracks"]}
    assert set(tracks) == {"microphone", "system"}, tracks
    channel_samples = {}
    for kind, wav_path in tracks.items():
        with wave.open(str(wav_path)) as audio:
            assert audio.getnchannels() == 1
            assert audio.getframerate() == 48000
            assert audio.getnframes() >= 48000 * 2
            channel_samples[kind] = struct.unpack(f"<{audio.getnframes()}h", audio.readframes(audio.getnframes()))
    rate = 48000
    frame_count = min(len(channel_samples["microphone"]), len(channel_samples["system"]))
    assert frame_count >= rate * 2
    samples = [
        sample
        for pair in zip(
            channel_samples["microphone"][:frame_count],
            channel_samples["system"][:frame_count],
            strict=True,
        )
        for sample in pair
    ]

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
        assert audio.getnchannels() == 1
        assert audio.getframerate() == rate
        assert audio.getnframes() >= rate * 2
        evidence["decoded"] = dict(
            frames=frame_count,
            opusFrames=audio.getnframes(),
            rate=rate,
            channels=2,
        )

    # Frames 1 s to 2 s, in the same 100 ms windows as the browser proofs: a
    # Pulse skew correction can flip a tone's phase mid-interval (signal_power).
    measured = stereo_tones(samples, rate, rate, rate * 2, (440, 880))
    evidence["rms"] = {side: measured.pop(side + "Rms") for side in ("left", "right")}
    amplitudes = measured
    evidence["amplitudes"] = amplitudes
    assert amplitudes["left440"] > 0.08 and amplitudes["right880"] > 0.08, amplitudes
    assert amplitudes["left880"] < 0.01 and amplitudes["right440"] < 0.01, amplitudes
    evidence["passed"] = True
    print(
        "PASS actual PulseAudio capture → NativeRecorder WAVs + published Opus: microphone 440 Hz, desktop 880 Hz, no cross-channel bleed"
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
        if recorder.pipeline is not None or recorder.starting or recorder.stopping or recorder._finishing:
            recorder.cancel_publication()
            recorder.stop()
            wait_until(
                lambda: (
                    recorder.pipeline is None
                    and not recorder.starting
                    and not recorder.stopping
                    and not recorder._finishing
                ),
                "native recorder cleanup",
                diagnostics=diagnostics,
            )
        recorder.close()
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
