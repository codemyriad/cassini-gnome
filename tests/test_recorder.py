# SPDX-License-Identifier: GPL-3.0-or-later
import os
import threading
import time

import pytest
from durable import DURABLE_BOUND

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from cassini_talk.native_recorder import NativeRecorder  # noqa: E402

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
    """Dispatch actual GLib sources with a guard for missing progress."""
    context, expired = GLib.MainContext.default(), []
    guard = GLib.timeout_add(int(DURABLE_BOUND * 1000), lambda: expired.append(True) or GLib.SOURCE_REMOVE)
    try:
        while not predicate() and not expired:
            context.iteration(True)
        assert predicate(), "Recorder did not deliver the expected completion event"
    finally:
        source = context.find_source_by_id(guard)
        if source is not None:
            source.destroy()


def notify_context():
    # A worker's real completion posts a source, so no wakeup is lost between
    # a predicate check and the main context starting its blocking iteration.
    GLib.idle_add(lambda: GLib.SOURCE_REMOVE)


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


def test_stop_never_waits_for_source_stream_locks_on_main_context(tmp_path, monkeypatch):
    recorder = NativeRecorder()
    release = threading.Event()
    main = threading.get_ident()
    threads, finished, errors = [], [], []
    recorder.connect("finished", lambda _r, path: finished.append(path))
    recorder.connect("error", lambda _r, error: errors.append(error))
    watchdogs = defer_stop_watchdog(recorder, monkeypatch)
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
            assert release.wait(DURABLE_BOUND), "Test did not release the held EOS worker"
        return original(element, event)

    try:
        recorder.start(tmp_path, test=True)
        dispatch_until(lambda: errors or (recorder.pipeline is not None and not getattr(recorder, "starting", False)))
        assert not errors
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
        elapsed_cpu_ns = time.thread_time_ns() - before_cpu
        if os.environ.get("CASSINI_PERF_GATE") == "1":
            assert elapsed_cpu_ns < 200_000_000, "Stop monopolized the calling thread"
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
        cleaned_up = False
        try:
            recorder.stop()
            if recorder.pipeline is not None and not recorder._finishing:
                recorder._begin_finish()
            dispatch_until(lambda: recorder.pipeline is None and not recorder.starting and not recorder._finishing)
            cleaned_up = True
        finally:
            for executor in (recorder._executor, recorder._bus_worker):
                executor.shutdown(wait=cleaned_up, cancel_futures=not cleaned_up)
