# SPDX-License-Identifier: GPL-3.0-or-later
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from cassini_viewer.recorder import Recorder, capture_pipeline  # noqa: E402

Gst.init(None)


def spin_until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for recorder"


def test_no_sources():
    with pytest.raises(ValueError):
        capture_pipeline(False, False)


@pytest.mark.parametrize("microphone,desktop,channels", [(True, True, 2), (True, False, 1), (False, True, 1)])
def test_capture_finalizes_playable_opus(tmp_path, microphone, desktop, channels):
    if not all(Gst.ElementFactory.find(e) for e in ("audiointerleave", "audiotestsrc", "opusenc", "oggmux", "opusdec")):
        pytest.skip("GStreamer recording plugins unavailable")
    path = tmp_path / "meeting.opus"
    recorder = Recorder()
    completed, errors = [], []
    recorder.connect("finished", lambda _r, p: completed.append(p))
    recorder.connect("error", lambda _r, e: errors.append(e))
    recorder.start(path, microphone, desktop, test=True)
    try:
        assert not path.exists()
        # Allow several buffers to pass through the live pipeline.
        deadline = time.monotonic() + 0.4
        spin_until(lambda: time.monotonic() >= deadline)
        recorder.stop()
        recorder.stop()  # Idempotent while EOS is pending.
        spin_until(lambda: completed or errors)
        assert not errors
        assert completed == [str(path)]
        data = path.read_bytes()
        assert data.startswith(b"OggS")
        offset = data.index(b"OpusHead")
        assert data[offset + 9] == channels
        pipeline = Gst.parse_launch("filesrc name=input ! oggdemux ! opusdec ! fakesink")
        pipeline.get_by_name("input").set_property("location", str(path))
        pipeline.set_state(Gst.State.PLAYING)
        try:
            message = pipeline.get_bus().timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            assert message and message.type == Gst.MessageType.EOS
        finally:
            pipeline.set_state(Gst.State.NULL)
        assert not list(tmp_path.glob(".cassini-*"))
    finally:
        recorder._cleanup()


def test_error_preserves_existing_destination(tmp_path):
    path = tmp_path / "existing.opus"
    path.write_bytes(b"original")
    recorder = Recorder()
    recorder.start(path, test=True)
    recorder._fail("simulated device disconnect")
    assert path.read_bytes() == b"original"
    assert recorder.pipeline is None
    assert not list(tmp_path.glob(".cassini-*"))


def test_capture_error_keeps_recoverable_audio(tmp_path):
    path = tmp_path / "existing.opus"
    path.write_bytes(b"original")
    recorder = Recorder()
    errors = []
    recorder.connect("error", lambda _r, message: errors.append(message))
    recorder.start(path, test=True)
    try:
        deadline = time.monotonic() + 0.5
        spin_until(lambda: time.monotonic() >= deadline)
        recorder._fail("Device disconnected")
        assert path.read_bytes() == b"original"
        recovered = list(tmp_path.glob("existing.opus.interrupted-*.opus"))
        assert len(recovered) == 1
        assert recovered[0].read_bytes().startswith(b"OggS")
        assert str(recovered[0]) in errors[0]
    finally:
        recorder._cleanup()
