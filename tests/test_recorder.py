# SPDX-License-Identifier: GPL-3.0-or-later
import time
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

from cassini_talk.recorder import Recorder, capture_pipeline  # noqa: E402

Gst.init(None)


def spin_until(predicate, timeout=12):
    # Legacy helper for external test modules; this file uses actual events.
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for recorder"


def dispatch_until(predicate):
    """Dispatch actual GLib sources; the outer test watchdog guards hangs."""
    context = GLib.MainContext.default()
    while not predicate():
        context.iteration(True)


def notify_context():
    # A worker's real completion posts a source, so no wakeup is lost between
    # a predicate check and the main context starting its blocking iteration.
    GLib.idle_add(lambda: GLib.SOURCE_REMOVE)


def encoded_audio(pipeline, errors, duration_ms=400):
    """Observe encoded media progress, rather than a wall-clock recording dwell."""
    iterator = pipeline.iterate_elements()
    while True:
        result, element = iterator.next()
        assert result == Gst.IteratorResult.OK, "No Opus encoder in the real capture pipeline"
        if element.get_factory().get_name() == "opusenc":
            break
    pad = element.get_static_pad("src")
    duration, ready = [0], [False]

    def packet(_pad, info):
        buffer = info.get_buffer()
        if buffer and not buffer.has_flags(Gst.BufferFlags.HEADER) and 0 < buffer.duration < Gst.CLOCK_TIME_NONE:
            duration[0] += buffer.duration
            if not ready[0] and duration[0] >= duration_ms * Gst.MSECOND:
                ready[0] = True
                notify_context()
        return Gst.PadProbeReturn.OK

    probe = pad.add_probe(Gst.PadProbeType.BUFFER, packet)
    try:
        dispatch_until(lambda: ready[0] or errors)
        assert not errors and ready[0]
    finally:
        pad.remove_probe(probe)


def defer_stop_watchdog(recorder, monkeypatch):
    """Keep the real source lifecycle, with expiry controlled by the timeout test."""
    context, timeout, watchdogs = GLib.MainContext.default(), GLib.timeout_add_seconds, []

    def arm(seconds, callback, *args):
        source_id = timeout(seconds, callback, *args)
        if callback == recorder._timed_out:
            source = context.find_source_by_id(source_id)
            source.set_ready_time(-1)
            watchdogs.append((seconds, source))
        return source_id

    monkeypatch.setattr(GLib, "timeout_add_seconds", arm)
    return watchdogs


def test_no_sources():
    with pytest.raises(ValueError):
        capture_pipeline(False, False)


@pytest.mark.parametrize("microphone,desktop,channels", [(True, True, 2), (True, False, 1), (False, True, 1)])
def test_capture_finalizes_playable_opus(tmp_path, monkeypatch, microphone, desktop, channels):
    if not all(Gst.ElementFactory.find(e) for e in ("audiointerleave", "audiotestsrc", "opusenc", "oggmux", "opusdec")):
        pytest.skip("GStreamer recording plugins unavailable")
    path = tmp_path / "meeting.opus"
    recorder = Recorder()
    completed, errors = [], []
    watchdogs = defer_stop_watchdog(recorder, monkeypatch)
    recorder.connect("finished", lambda _r, p: completed.append(p))
    recorder.connect("error", lambda _r, e: errors.append(e))
    recorder.start(path, microphone, desktop, test=True)
    try:
        assert not path.exists()
        encoded_audio(recorder.pipeline, errors)
        recorder.stop()
        recorder.stop()  # Idempotent while EOS is pending.
        dispatch_until(lambda: completed or errors)
        assert not errors
        assert completed == [str(path)]
        assert len(watchdogs) == 1 and watchdogs[0][0] == 10 and watchdogs[0][1].is_destroyed()
        data = path.read_bytes()
        assert data.startswith(b"OggS")
        offset = data.index(b"OpusHead")
        assert data[offset + 9] == channels
        pipeline = Gst.parse_launch("filesrc name=input ! oggdemux ! opusdec ! fakesink")
        pipeline.get_by_name("input").set_property("location", str(path))
        pipeline.set_state(Gst.State.PLAYING)
        try:
            message = pipeline.get_bus().timed_pop_filtered(
                Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR | Gst.MessageType.EOS
            )
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
        encoded_audio(recorder.pipeline, errors, duration_ms=500)
        temporary = Path(recorder._temporary)
        watch = Gio.File.new_for_path(str(temporary)).monitor_file(Gio.FileMonitorFlags.NONE, None)
        handler = watch.connect("changed", lambda *_: None)
        try:
            dispatch_until(lambda: errors or temporary.stat().st_size >= 4)
            assert not errors and temporary.read_bytes().startswith(b"OggS")
        finally:
            watch.disconnect(handler)
            watch.cancel()
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
        dispatch_until(lambda: bool(errors))
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
    release = threading.Event()
    main = threading.get_ident()
    threads, finished, errors = [], [], []
    recorder.connect("finished", lambda _r, path: finished.append(path))
    recorder.connect("error", lambda _r, error: errors.append(error))
    watchdogs = defer_stop_watchdog(recorder, monkeypatch)
    if kind == "tracks":
        sample = recorder._sample
        have_pcm = threading.Event()

        def observed_sample(*args):
            result = sample(*args)
            if not have_pcm.is_set():
                notify_context()
            return result

        monkeypatch.setattr(recorder, "_sample", observed_sample)
    original = Gst.Element.send_event

    def slow_event(element, event):
        if event.type == Gst.EventType.EOS:
            thread = threading.get_ident()
            threads.append(thread)
            notify_context()
            assert thread != main, "EOS delivery ran on the main context"
            release.wait()
        return original(element, event)

    try:
        recorder.start(tmp_path / "meeting.opus" if kind == "opus" else tmp_path, test=True)
        dispatch_until(lambda: errors or (recorder.pipeline is not None and not getattr(recorder, "starting", False)))
        assert not errors
        if kind == "tracks":
            dispatch_until(
                lambda: (
                    errors
                    or (
                        len(recorder.session.writers) == 2
                        and all(writer.frames > 0 for writer in recorder.session.writers.values())
                    )
                )
            )
            assert not errors
            have_pcm.set()
        monkeypatch.setattr(Gst.Element, "send_event", slow_event)
        before_cpu = time.thread_time_ns()
        recorder.stop()
        # Keep the original 200 ms synchronous-work ceiling as thread CPU.
        assert time.thread_time_ns() - before_cpu < 200_000_000, "Stop monopolized the calling thread"
        assert not release.is_set() and all(thread != main for thread in threads)
        dispatch_until(lambda: len(threads) == 2 or errors)
        assert not errors and len(set(threads)) == 2, "Both inputs must finish on independent workers"
        idle = []
        GLib.idle_add(lambda: idle.append(True) or GLib.SOURCE_REMOVE)
        dispatch_until(lambda: bool(idle))
        assert threads and all(thread != main for thread in threads)
        assert not finished and not release.is_set()
        assert len(watchdogs) == 1 and watchdogs[0][0] == 10
        assert not watchdogs[0][1].is_destroyed()
        release.set()
        dispatch_until(lambda: bool(finished or errors))
        assert not errors and len(finished) == 1
        assert watchdogs[0][1].is_destroyed()
    finally:
        release.set()
        if kind == "opus":
            recorder._cleanup()
        else:
            recorder.stop()
            dispatch_until(lambda: recorder.pipeline is None and not recorder.starting and not recorder._finishing)
            recorder._executor.shutdown(wait=True)
            recorder._bus_worker.shutdown(wait=True)
