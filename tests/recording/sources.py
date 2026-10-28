#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private real Pulse: independent source stop/restart, new WAV spans and offsets."""

import json
import math
import os
import struct
import sys
import threading
import time
import traceback
from pathlib import Path

if os.environ.get("CASSINI_ISOLATED_AUDIO") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
if not os.environ.get("PULSE_SERVER", "").startswith("unix:/tmp/cassini-audio-"):
    raise SystemExit("A private Pulse socket is required")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from readiness import wait_until  # noqa: E402

from cassini_viewer.native_recorder import NativeRecorder  # noqa: E402

Gst.init(None)
artifacts = Path(os.environ["CASSINI_TEST_ARTIFACTS"])
native = NativeRecorder()
errors, producers = [], []
evidence = {}
native.connect("error", lambda _r, message: errors.append(message))


def diagnostics():
    return dict(
        errors=errors,
        manifest=native.session.manifest if native.session else None,
        changing=native._changing_sources,
        stopping=native.stopping,
        pipeline=bool(native.pipeline),
        trackIds=dict(native._track_ids),
    )


def frames(kind):
    with native._lock:
        identifier = native._track_ids.get(kind)
        return native.session.writers[identifier].frames if identifier else 0


def select(me, others):
    future = native.select(me, others)
    wait_until(
        lambda: future.done() and not native._changing_sources, "source choice checkpointed", diagnostics=diagnostics
    )
    state = future.result()
    assert (state.me, state.others) == (me, others)
    assert not errors


def power(file, frequency):
    data = file.read_bytes()[44:]
    samples = struct.unpack("<" + "h" * (len(data) // 2), data)[-24000:]
    assert len(samples) == 24000
    real = sum(v / 32768 * math.cos(2 * math.pi * frequency * i / 48000) for i, v in enumerate(samples))
    imaginary = sum(v / 32768 * math.sin(2 * math.pi * frequency * i / 48000) for i, v in enumerate(samples))
    return (real * real + imaginary * imaginary) / len(samples) ** 2


try:
    for device, frequency in (("cassini_microphone", 660), ("cassini_desktop", 880)):
        producer = Gst.parse_launch(
            f"audiotestsrc is-live=true freq={frequency} volume=0.25 ! audioconvert ! audioresample ! "
            f"audio/x-raw,rate=48000,channels=1 ! pulsesink device={device} sync=false"
        )
        producers.append(producer)
        assert producer.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        assert producer.get_state(5 * Gst.SECOND).state == Gst.State.PLAYING
    native.start(artifacts)
    wait_until(
        lambda: native.session is not None and frames("microphone") >= 96000 and frames("system") >= 96000,
        "two seconds in both initial sources",
        diagnostics=diagnostics,
    )
    session = native.session
    initial = {t["kind"]: t["file"] for t in session.manifest["tracks"]}
    select(True, False)
    system_frozen = (session.directory / initial["system"]).read_bytes()
    before = frames("microphone")
    wait_until(lambda: frames("microphone") >= before + 48000, "microphone continues alone", diagnostics=diagnostics)
    assert (session.directory / initial["system"]).read_bytes() == system_frozen
    assert native.pipeline.get_by_name("system") is None, "Off source must release its Pulse stream"
    select(False, True)
    microphone_frozen = (session.directory / initial["microphone"]).read_bytes()
    wait_until(lambda: frames("system") >= 96000, "new system span has two seconds", diagnostics=diagnostics)
    assert native.pipeline.get_by_name("microphone") is None
    assert (session.directory / initial["microphone"]).read_bytes() == microphone_frozen
    select(True, True)
    wait_until(lambda: frames("microphone") >= 96000, "new microphone span has two seconds", diagnostics=diagnostics)
    assert (session.directory / initial["system"]).read_bytes() == system_frozen
    assert (session.directory / initial["microphone"]).read_bytes() == microphone_frozen
    assert len(session.manifest["tracks"]) == 4
    for track in session.manifest["tracks"]:
        target = 660 if track["kind"] == "microphone" else 880
        wanted = power(session.directory / track["file"], target)
        bleed = power(session.directory / track["file"], 880 if target == 660 else 660)
        assert wanted > 0.001 and bleed < wanted * 0.01, (wanted, bleed, track)
        if track["id"] not in ("microphone", "system"):
            assert track["startOffsetMs"] > 2000, track
    # A Stop during an accepted but queued transition stays asynchronous and
    # finishes the newly selected sources before exposing the completed folder.
    entered, release = threading.Event(), threading.Event()
    blocker = native._executor.submit(lambda: (entered.set(), release.wait(5)))
    assert entered.wait(1)
    future = native.select(True, False)
    started = time.monotonic()
    native.stop()
    assert time.monotonic() - started < 0.2
    release.set()
    blocker.result(2)
    wait_until(
        lambda: native.pipeline is None and not native._finishing,
        "Stop joins source change and finalizes",
        diagnostics=diagnostics,
    )
    future.result()
    assert not errors and session.manifest["state"] == "completed"
    assert all(t["ended"] and t["endOffsetMs"] >= t["startOffsetMs"] for t in session.manifest["tracks"])
    saved = json.loads((session.directory / "session.json").read_text())
    assert saved == session.manifest
    assert len(saved["events"]) == 9
    evidence.update(
        passed=True,
        releasedOffSources=True,
        separateWavs=True,
        frozenEndedBytes=True,
        originalClockOffsets=True,
        tonesAndBleed=True,
        stopDuringChange=True,
        manifest=saved,
    )
    print(
        "PASS real Pulse source changes: released streams, frozen WAVs, new offsets, tone isolation and asynchronous Stop"
    )
except Exception:
    evidence.update(failure=traceback.format_exc(), state=diagnostics())
    if native.pipeline is not None:
        (artifacts / "sources-failed.dot").write_text(
            Gst.debug_bin_to_dot_data(native.pipeline, Gst.DebugGraphDetails.ALL)
        )
    raise
finally:
    if native.pipeline is not None:
        native.stop()
        wait_until(lambda: native.pipeline is None, "source test cleanup", diagnostics=diagnostics)
    for producer in producers:
        producer.set_state(Gst.State.NULL)
    native.close()
    (artifacts / "sources-results.json").write_text(json.dumps(evidence, indent=2) + "\n")
