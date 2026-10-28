#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real private Pulse microphone copies belong to independent browser sessions."""

import base64
import json
import math
import os
import struct
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
pulse = os.environ.get("PULSE_SERVER", "")
if not pulse.startswith("unix:/tmp/cassini-audio-") or not pulse.endswith("/pulse.sock"):
    raise SystemExit("A private Pulse socket is required")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from readiness import wait_until  # noqa: E402

from cassini_viewer.browser_audio_proxy import BrowserAudioProxy  # noqa: E402
from cassini_viewer.browser_service import BrowserService  # noqa: E402

Gst.init(None)
output = Path(os.environ["CASSINI_TEST_ARTIFACTS"])
service = BrowserService(output / "browser.json", capture_parent=output / "library")
path = Path(os.environ["XDG_RUNTIME_DIR"]) / "mic-proof/browser.sock"
ready = threading.Event()
thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
thread.start()
assert ready.wait(2)
producer = Gst.parse_launch(
    'audiotestsrc is-live=true freq=660 volume=0.25 ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! pulsesink device=cassini_microphone client-name="Cassini mic fixture"'
)
evidence = {"cases": []}
clients = []


def diagnostics():
    return service.snapshot()


def microphone_frames(capture):
    with capture.lock:
        return next((t["frames"] for t in capture.host.session.manifest["tracks"] if t["kind"] == "microphone"), 0)


def tone(directory, track, hz):
    data = (directory / track["file"]).read_bytes()[44:]
    values = struct.unpack("<" + "h" * (len(data) // 2), data)
    assert len(values) >= 12000
    values = values[-12000:]
    re = sum(value / 32768 * math.cos(2 * math.pi * hz * i / 48000) for i, value in enumerate(values))
    im = sum(value / 32768 * math.sin(2 * math.pi * hz * i / 48000) for i, value in enumerate(values))
    return (re * re + im * im) / (12000 * 12000)


try:
    assert producer.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
    state = producer.get_state(5 * Gst.SECOND)
    assert state.state == Gst.State.PLAYING, state
    for second_mic in (True, False):
        captures = []
        connections = []
        for selected in (True, second_mic):
            client = BrowserAudioProxy(path=path)
            clients.append(client)
            connections.append(client)
            identifier = str(uuid.uuid4())
            client.handle(
                dict(
                    type="start",
                    version=1,
                    independent=True,
                    captureId=identifier,
                    microphone=selected,
                    timelineOriginMs=int(time.time() * 1000),
                    title="Private browser microphone proof",
                )
            )
            capture = service.captures[identifier]
            captures.append(capture)
            client.handle(dict(type="track", id="remote", label="Other", kind="remote", sampleRate=48000, channels=1))
            client.handle(
                dict(
                    type="audio",
                    id="remote",
                    sequence=0,
                    offsetMs=200,
                    pcm=base64.b64encode(b"\x00\x00" * 48000).decode(),
                )
            )
        selected = [c for c in captures if c.microphone is not None]
        assert len(selected) == 1 + int(second_mic)
        wait_until(
            lambda selected=selected: all(microphone_frames(c) >= 96000 for c in selected),
            "two seconds in every selected microphone copy",
            diagnostics=diagnostics,
        )
        for capture in selected:
            (output / (capture.capture_id + ".dot")).write_text(
                Gst.debug_bin_to_dot_data(capture.microphone.pipeline, Gst.DebugGraphDetails.ALL)
            )
        connections[0].handle(dict(type="stop"))
        first_size = (captures[0].directory / "track-microphone.wav").stat().st_size
        if second_mic:
            before = microphone_frames(captures[1])
            wait_until(
                lambda capture=captures[1], before=before: microphone_frames(capture) >= before + 48000,
                "second microphone continues after first Stop",
                diagnostics=diagnostics,
            )
        assert (captures[0].directory / "track-microphone.wav").stat().st_size == first_size
        connections[1].handle(dict(type="stop"))
        results = []
        for selected, capture in zip((True, second_mic), captures, strict=True):
            manifest = json.loads((capture.directory / "session.json").read_text())
            assert manifest["state"] == "completed", manifest
            tracks = [t for t in manifest["tracks"] if t["kind"] == "microphone"]
            assert len(tracks) == int(selected)
            if selected:
                track = tracks[0]
                assert track["frames"] >= 96000 and 0 <= track["startOffsetMs"] < 10000
                wanted = tone(capture.directory, track, 660)
                bleed = tone(capture.directory, track, 880)
                assert wanted > 0.001 and bleed < wanted * 0.01, (wanted, bleed)
                assert track["labelSource"] == "device"
                results.append(
                    dict(frames=track["frames"], startOffsetMs=track["startOffsetMs"], power660=wanted, bleed880=bleed)
                )
        evidence["cases"].append(
            dict(secondMic=second_mic, microphones=results, firstStoppedWithoutStoppingSecond=True)
        )
        for client in connections:
            client.close()
    evidence["passed"] = True
    print(
        "PASS real per-session microphone copies: both selected, then only the first; isolated Pulse tone, monotonic offsets, independent Stop"
    )
except Exception:
    evidence["failure"] = traceback.format_exc()
    evidence["state"] = diagnostics()
    raise
finally:
    for client in clients:
        client.close()
    producer.set_state(Gst.State.NULL)
    service.closed.set()
    thread.join(2)
    (output / "browser-microphone.json").write_text(json.dumps(evidence, indent=2) + "\n")
