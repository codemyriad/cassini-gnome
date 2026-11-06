# SPDX-License-Identifier: GPL-3.0-or-later
"""Readiness guards use finite generated buffers, no display or audio server."""

import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

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


def test_capture_files_waits_for_an_inflight_checkpoint_to_commit(tmp_path, monkeypatch):
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    lock = threading.RLock()
    entered, release, requested = threading.Event(), threading.Event(), threading.Event()
    errors, result = [], {}
    original = Path.replace

    def replace(path, target):
        if path == session.directory / "session.tmp":
            entered.set()
            assert release.wait(3), "Test did not release the checkpoint rename"
        return original(path, target)

    class WriterLock:
        def __enter__(self):
            requested.set()
            lock.acquire()

        def __exit__(self, *_):
            lock.release()

    capture = SimpleNamespace(lock=WriterLock(), directory=session.directory)
    files = readiness.capture_files(capture)
    requested.clear()
    monkeypatch.setattr(Path, "replace", replace)

    def checkpoint():
        try:
            with lock:
                session.checkpoint()
        except BaseException as error:
            errors.append(error)

    def inspect():
        try:
            result["files"] = readiness.capture_files(capture)
        except BaseException as error:
            errors.append(error)

    writer = threading.Thread(target=checkpoint)
    observer = threading.Thread(target=inspect)
    writer.start()
    try:
        assert entered.wait(3), "Checkpoint did not reach its atomic rename"
        # This is the old assertion's race: the raw listing really sees the
        # temporary file while the healthy source's checkpoint is in flight.
        assert {path.name for path in capture.directory.iterdir()} == files | {"session.tmp"}
        observer.start()
        assert requested.wait(3), "Snapshot did not wait on the writer boundary"
        assert "files" not in result
    finally:
        release.set()
        writer.join(3)
        if observer.ident is not None:
            observer.join(3)
    assert not writer.is_alive() and not observer.is_alive() and not errors, errors
    assert result["files"] == files
    assert json.loads((session.directory / "session.json").read_text()) == session.manifest
    session.finish()
