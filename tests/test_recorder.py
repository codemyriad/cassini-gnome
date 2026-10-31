# SPDX-License-Identifier: GPL-3.0-or-later
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from cassini_talk.recorder import Recorder, capture_pipeline  # noqa: E402

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


def test_bus_error_cleans_pipeline_and_reports_once(tmp_path):
    recorder = Recorder()
    errors = []
    recorder.connect("error", lambda _r, message: errors.append(message))
    recorder.start(tmp_path / "failed.opus", test=True)
    try:
        message = Gst.Message.new_error(
            recorder.pipeline, GLib.Error("synthetic source failure"), "injected device error"
        )
        recorder.pipeline.get_bus().post(message)
        spin_until(lambda: bool(errors))
        assert len(errors) == 1 and "synthetic source failure" in errors[0]
        assert recorder.pipeline is None and not recorder.stopping
    finally:
        recorder._cleanup()


def test_stop_timeout_reports_error_without_waiting_ten_seconds(tmp_path, monkeypatch):
    recorder = Recorder()
    errors, callbacks = [], []
    recorder.connect("error", lambda _r, message: errors.append(message))
    monkeypatch.setattr(GLib, "timeout_add_seconds", lambda seconds, callback: callbacks.append(callback) or 0)
    recorder.start(tmp_path / "timeout.opus", test=True)
    try:
        recorder.stop()
        assert len(callbacks) == 1
        callbacks[0]()  # Exercise the timeout before dispatching any EOS bus message.
        assert len(errors) == 1 and "Timed out" in errors[0]
        assert recorder.pipeline is None and recorder._timeout == 0
    finally:
        recorder._cleanup()


def test_pipeline_construction_failure_leaves_no_partial_output(tmp_path, monkeypatch):
    recorder = Recorder()

    def fail(_description):
        raise GLib.Error("missing capture plugin")

    monkeypatch.setattr(Gst, "parse_launch", fail)
    with pytest.raises(GLib.Error, match="missing capture plugin"):
        recorder.start(tmp_path / "failure.opus", test=True)
    assert recorder.pipeline is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["opus", "tracks"])
def test_stop_never_waits_for_source_stream_locks_on_main_context(tmp_path, monkeypatch, kind):
    import threading

    from cassini_talk.native_recorder import NativeRecorder

    recorder = Recorder() if kind == "opus" else NativeRecorder()
    entered, release = threading.Event(), threading.Event()
    threads, finished, errors = [], [], []
    recorder.connect("finished", lambda _r, path: finished.append(path))
    recorder.connect("error", lambda _r, error: errors.append(error))
    recorder.start(tmp_path / "meeting.opus" if kind == "opus" else tmp_path, test=True)
    spin_until(lambda: recorder.pipeline is not None)
    if kind == "tracks":
        # This scenario tests two active stream locks, rather than empty
        # sources which are retired without EOS before their first sample.
        spin_until(lambda: all(writer.frames > 0 for writer in recorder.session.writers.values()))
    original = Gst.Element.send_event

    def slow_event(element, event):
        if event.type == Gst.EventType.EOS:
            threads.append(threading.get_ident())
            entered.set()
            assert release.wait(3), "Source event never released"
        return original(element, event)

    monkeypatch.setattr(Gst.Element, "send_event", slow_event)
    try:
        before = time.monotonic()
        recorder.stop()
        assert time.monotonic() - before < 0.2, "Stop waited on a streaming lock"
        spin_until(entered.is_set, timeout=1)
        spin_until(lambda: len(threads) == 2)
        assert len(set(threads)) == 2, "Both interleave inputs must be able to finish independently"
        idle = []
        GLib.idle_add(lambda: idle.append(True) or False)
        spin_until(lambda: bool(idle))
        assert threads and all(thread != threading.get_ident() for thread in threads)
        # Both source events may be blocked; GTK and its stop watchdog still run.
        release.set()
        spin_until(lambda: bool(finished or errors))
        assert not errors and len(finished) == 1
    finally:
        release.set()
        if kind == "opus":
            recorder._cleanup()
        else:
            recorder.stop()
            spin_until(lambda: recorder.pipeline is None and not recorder._finishing)
            recorder._executor.shutdown(wait=True)
