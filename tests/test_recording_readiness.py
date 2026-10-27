# SPDX-License-Identifier: GPL-3.0-or-later
"""Readiness guards use finite generated buffers, no display or audio server."""

import importlib.util
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

spec = importlib.util.spec_from_file_location("recording_readiness", Path(__file__).parent / "recording/readiness.py")
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)
Gst.init(None)


@pytest.mark.parametrize("wave,audible", [("sine", True), ("silence", False)])
def test_readiness_requires_actual_complete_frames_and_audible_input(wave, audible):
    pipeline = Gst.parse_launch(
        f"audiotestsrc name=source wave={wave} num-buffers=4 samplesperbuffer=480 ! "
        "audio/x-raw,format=S16LE,rate=48000,channels=1 ! fakesink sync=false"
    )
    progress = readiness.PCMProgress(pipeline.get_by_name("source").get_static_pad("src"), observe_audio=True)
    try:
        assert not progress.reached(0.04)  # PLAYING request/time alone is not PCM.
        pipeline.set_state(Gst.State.PLAYING)
        readiness.wait_until(lambda: progress.reached(0.04), "finite synthetic frames", timeout=3)
        assert progress.snapshot() == dict(
            frames=1920, audibleFrames=1920 if audible else 0, sampleRate=48000, channels=1
        )
        assert bool(progress.reached(0.04, audible=True)) == audible
        assert not progress.reached(0.05)
    finally:
        progress.close()
        pipeline.set_state(Gst.State.NULL)
        progress.close()


def test_silent_buffer_resets_continuous_audible_progress():
    pipeline = Gst.parse_launch(
        'appsrc name=source caps="audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved" ! fakesink sync=false'
    )
    source = pipeline.get_by_name("source")
    progress = readiness.PCMProgress(source.get_static_pad("src"), observe_audio=True)
    try:
        pipeline.set_state(Gst.State.PLAYING)
        for index, data in enumerate([b"\x01\x00" * 480, bytes(960), b"\x01\x00" * 480], 1):
            assert source.emit("push-buffer", Gst.Buffer.new_wrapped(data)) == Gst.FlowReturn.OK
            readiness.wait_until(
                lambda expected=index * 480: progress.snapshot()["frames"] >= expected, "finite PCM chunk"
            )
            assert progress.snapshot()["audibleFrames"] == (0 if index == 2 else 480)
        assert not progress.reached(0.02, audible=True)
    finally:
        progress.close()
        pipeline.set_state(Gst.State.NULL)


def test_unready_source_has_bounded_timeout_with_diagnostics():
    with pytest.raises(AssertionError, match=r"Timed out.*silent source.*frames.*0"):
        readiness.wait_until(lambda: False, "silent source", timeout=0.02, diagnostics=lambda: {"frames": 0})


def test_readiness_does_not_swallow_source_errors():
    def disconnected():
        raise RuntimeError("source disconnected")

    with pytest.raises(RuntimeError, match="source disconnected"):
        readiness.wait_until(disconnected, "failed source")
