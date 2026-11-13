# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic teardown and fixture-probe contracts for recording tests."""

import importlib.util
import math
import shutil
import struct
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _load_recording_module(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "tests/recording" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drain_recording_app_blocks_cleanup_until_encoder_and_workers_finish(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    from cassini_talk import library_publication

    teardown = _load_recording_module("teardown.py", "recording_teardown")

    private = tempfile.TemporaryDirectory()
    sessions = Path(private.name) / "Library/.cassini/sessions/Recording-1"
    sessions.mkdir(parents=True)

    encoder_entered = threading.Event()
    recovery_entered = threading.Event()
    editor_entered = threading.Event()
    release_workers = threading.Event()
    pub_cancelled = threading.Event()
    drain_finished = threading.Event()
    drain_errors = []

    def encoder_task():
        temp = sessions / "encode.tmp"
        temp.write_bytes(b"encoding")
        encoder_entered.set()
        assert release_workers.wait(5)
        temp.unlink()

    def recovery_task():
        temp = sessions / "recover.tmp"
        temp.write_bytes(b"recovering")
        recovery_entered.set()
        assert release_workers.wait(5)
        temp.unlink()

    def editor_task():
        temp = sessions / "meeting.json.tmp"
        temp.write_bytes(b"{}")
        editor_entered.set()
        assert release_workers.wait(5)
        temp.unlink()

    recovery_executor = ThreadPoolExecutor(max_workers=1)
    editor_executor = ThreadPoolExecutor(max_workers=1)
    monitor_executor = ThreadPoolExecutor(max_workers=1)
    trash_executor = ThreadPoolExecutor(max_workers=1)
    recorder_executor = ThreadPoolExecutor(max_workers=1)
    bus_executor = ThreadPoolExecutor(max_workers=1)
    record_start_executor = ThreadPoolExecutor(max_workers=1)

    library_publication._ENCODER.submit(encoder_task)
    recovery_executor.submit(recovery_task)
    editor_executor.submit(editor_task)

    assert encoder_entered.wait(5)
    assert recovery_entered.wait(5)
    assert editor_entered.wait(5)

    recovery_cancelled = threading.Event()
    recorder = SimpleNamespace(
        pipeline=None,
        starting=False,
        stopping=False,
        _finishing=False,
        _executor=recorder_executor,
        _bus_worker=bus_executor,
        cancel_publication=pub_cancelled.set,
        close=lambda: (recorder_executor.shutdown(wait=False), bus_executor.shutdown(wait=False)),
    )
    window_closed = threading.Event()
    window = SimpleNamespace(
        recorder=recorder,
        monitor=SimpleNamespace(_executor=monitor_executor, close=lambda: monitor_executor.shutdown(wait=False)),
        search_page=None,
        export_dialog=None,
        _activity_source=None,
        _import_executor=None,
        player=SimpleNamespace(shutdown=lambda: None),
        close=window_closed.set,
    )
    begin_exit_called = threading.Event()
    app = SimpleNamespace(
        _capture_exiting=False,
        _capture_background_saving=False,
        recorder=recorder,
        get_windows=lambda: [] if window_closed.is_set() else [window],
        library_recovery=SimpleNamespace(
            cancelled=recovery_cancelled,
            executor=recovery_executor,
            close=lambda: (recovery_cancelled.set(), recovery_executor.shutdown(wait=False)),
        ),
        comfort=SimpleNamespace(
            editor=editor_executor,
            executor=None,
            close=lambda: editor_executor.shutdown(wait=False),
        ),
        meeting_trash=SimpleNamespace(executor=trash_executor),
        _record_start_executor=record_start_executor,
        begin_exit=begin_exit_called.set,
    )

    def run_drain():
        try:
            teardown.drain_recording_app(app, timeout=5)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            drain_errors.append(exc)
        finally:
            drain_finished.set()

    waiter = threading.Thread(target=run_drain)
    waiter.start()

    assert pub_cancelled.wait(5)
    assert recovery_cancelled.wait(5)
    assert window_closed.wait(5)
    assert begin_exit_called.wait(5)
    assert not drain_finished.is_set(), "drain_recording_app returned while session writers were still active"

    release_workers.set()
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert not drain_errors, drain_errors
    assert list(sessions.iterdir()) == []
    private.cleanup()
    assert not Path(private.name).exists()

    # The shared process-wide publication executor must remain live for subsequent tests.
    assert library_publication._ENCODER.submit(lambda: "alive").result(timeout=5) == "alive"


def test_recording_app_teardown_orders_error_hook_before_drain_and_cleanup(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    teardown = _load_recording_module("teardown.py", "recording_teardown")

    events = []
    worker_entered = threading.Event()
    release_worker = threading.Event()
    recovery_executor = ThreadPoolExecutor(max_workers=1)
    managed_dir = None

    app = SimpleNamespace(
        _capture_exiting=False,
        _capture_background_saving=False,
        recorder=None,
        get_windows=lambda: [SimpleNamespace(close=lambda: events.append("window_close"))],
        library_recovery=SimpleNamespace(cancelled=threading.Event(), executor=recovery_executor),
    )

    with (
        pytest.raises(RuntimeError, match="synthetic scenario failure"),
        teardown.recording_app_teardown(
            app,
            on_error=lambda: events.append(("on_error", Path(managed_dir).is_dir())),
            stop_active=lambda: (events.append("stop_active"), release_worker.set()),
            timeout=5,
        ) as directory,
    ):
        managed_dir = directory
        session_file = Path(directory) / "active.tmp"

        def active_write():
            session_file.write_bytes(b"busy")
            worker_entered.set()
            assert release_worker.wait(5)
            session_file.unlink()
            events.append("worker_done")

        recovery_executor.submit(active_write)
        assert worker_entered.wait(5)
        raise RuntimeError("synthetic scenario failure")

    assert events == [("on_error", True), "stop_active", "window_close", "worker_done"]
    assert managed_dir is not None and not Path(managed_dir).exists()


def test_drain_recording_app_respects_shared_deadline_and_reports_outstanding_workers(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    from cassini_talk import library_publication

    teardown = _load_recording_module("teardown.py", "recording_teardown")

    recovery_entered = threading.Event()
    trash_entered = threading.Event()
    encoder_entered = threading.Event()
    release_all = threading.Event()
    recovery_done = threading.Event()
    trash_done = threading.Event()
    encoder_done = threading.Event()

    recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-recovery")
    trash_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-trash")

    def blocked(entered, done):
        entered.set()
        assert release_all.wait(5)
        done.set()

    try:
        recovery_executor.submit(blocked, recovery_entered, recovery_done)
        trash_executor.submit(blocked, trash_entered, trash_done)
        library_publication._ENCODER.submit(blocked, encoder_entered, encoder_done)
        assert recovery_entered.wait(5)
        assert trash_entered.wait(5)
        assert encoder_entered.wait(5)

        app = SimpleNamespace(
            _capture_exiting=False,
            _capture_background_saving=False,
            recorder=None,
            get_windows=lambda: [],
            library_recovery=SimpleNamespace(cancelled=threading.Event(), executor=recovery_executor),
            meeting_trash=SimpleNamespace(executor=trash_executor),
        )

        with pytest.raises(TimeoutError) as exc_info:
            teardown.drain_recording_app(app, timeout=0, clock=lambda: 100.0)

        message = str(exc_info.value)
        assert "library_recovery.executor" in message
        assert "meeting_trash.executor" in message
        assert "library_publication._ENCODER" in message
        assert not recovery_done.is_set()
        assert not trash_done.is_set()
        assert not encoder_done.is_set()
    finally:
        release_all.set()
        assert recovery_done.wait(5)
        assert trash_done.wait(5)
        assert encoder_done.wait(5)
        recovery_executor.shutdown(wait=True)
        trash_executor.shutdown(wait=True)
        assert library_publication._ENCODER.submit(lambda: "ready").result(timeout=5) == "ready"


def test_recording_app_teardown_retains_directory_when_worker_exceeds_deadline(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    teardown = _load_recording_module("teardown.py", "recording_teardown")

    writer_entered = threading.Event()
    release_writer = threading.Event()
    writer_finished = threading.Event()
    recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stuck-recovery")
    retained_dir = None

    app = SimpleNamespace(
        _capture_exiting=False,
        _capture_background_saving=False,
        recorder=None,
        get_windows=lambda: [],
        library_recovery=SimpleNamespace(cancelled=threading.Event(), executor=recovery_executor),
    )

    try:
        with (
            pytest.raises(TimeoutError, match="library_recovery.executor") as exc_info,
            teardown.recording_app_teardown(
                app,
                prefix="cassini-retained-timeout-",
                timeout=0,
                clock=lambda: 50.0,
            ) as directory,
        ):
            retained_dir = Path(directory)
            active_file = retained_dir / "active.tmp"

            def stuck_write():
                active_file.write_bytes(b"in-flight")
                writer_entered.set()
                assert release_writer.wait(5)
                # Directory must still exist while the writer finishes!
                assert active_file.read_bytes() == b"in-flight"
                active_file.unlink()
                writer_finished.set()

            recovery_executor.submit(stuck_write)
            assert writer_entered.wait(5)

        assert retained_dir is not None and retained_dir.is_dir()
        assert (retained_dir / "active.tmp").is_file()
        assert any("Retained recording test directory" in note for note in getattr(exc_info.value, "__notes__", ()))
    finally:
        release_writer.set()
        assert writer_finished.wait(5)
        recovery_executor.shutdown(wait=True)
        if retained_dir is not None and retained_dir.exists():
            shutil.rmtree(retained_dir)


def test_recording_app_teardown_drains_when_stop_active_raises_and_preserves_both_failures(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    teardown = _load_recording_module("teardown.py", "recording_teardown")

    # Subcase A: stop_active() raises after a scenario failure; drain_recording_app() must still run,
    # join the background writer, retain the directory (because stopping failed), and preserve both errors.
    writer_entered = threading.Event()
    drain_entered = threading.Event()
    writer_done = threading.Event()
    recovery_executor = ThreadPoolExecutor(max_workers=1)
    retained_a = None

    app_a = SimpleNamespace(
        _capture_exiting=False,
        _capture_background_saving=False,
        recorder=None,
        get_windows=lambda: [SimpleNamespace(close=drain_entered.set)],
        library_recovery=SimpleNamespace(cancelled=threading.Event(), executor=recovery_executor),
    )

    def failing_stop():
        raise AssertionError("capture finalization exceeded readiness timeout")

    try:
        with (
            pytest.raises(ExceptionGroup) as group_a,
            teardown.recording_app_teardown(
                app_a,
                prefix="cassini-stop-raises-a-",
                stop_active=failing_stop,
                timeout=5,
            ) as directory,
        ):
            retained_a = Path(directory)

            def drainable_write():
                target = retained_a / "session.tmp"
                target.write_bytes(b"draining")
                writer_entered.set()
                assert drain_entered.wait(5), "drain_recording_app never ran after stop_active raised"
                assert target.is_file()
                target.unlink()
                writer_done.set()

            recovery_executor.submit(drainable_write)
            assert writer_entered.wait(5)
            raise RuntimeError("primary scenario failure")

        assert writer_done.is_set()
        assert retained_a is not None and retained_a.is_dir()
        sub_a = group_a.value.exceptions
        assert len(sub_a) == 2
        assert isinstance(sub_a[0], RuntimeError) and "primary scenario failure" in str(sub_a[0])
        assert isinstance(sub_a[1], AssertionError) and "capture finalization" in str(sub_a[1])
    finally:
        recovery_executor.shutdown(wait=True)
        if retained_a is not None and retained_a.exists():
            shutil.rmtree(retained_a)

    # Subcase B: stop_active() raises with an active pipeline + unstopped writer at deadline;
    # directory is retained while the writer stays active, and all three failures are preserved.
    stuck_entered = threading.Event()
    release_stuck = threading.Event()
    stuck_done = threading.Event()
    stuck_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="unstopped-writer")
    retained_b = None

    busy_recorder = SimpleNamespace(
        pipeline=object(),
        starting=False,
        stopping=True,
        _finishing=False,
        _executor=None,
        _bus_worker=None,
        cancel_publication=lambda: None,
    )
    app_b = SimpleNamespace(
        _capture_exiting=False,
        _capture_background_saving=False,
        recorder=busy_recorder,
        get_windows=lambda: [],
        library_recovery=SimpleNamespace(cancelled=threading.Event(), executor=stuck_executor),
    )

    try:
        with (
            pytest.raises(ExceptionGroup) as group_b,
            teardown.recording_app_teardown(
                app_b,
                prefix="cassini-stop-raises-b-",
                stop_active=failing_stop,
                timeout=0,
                clock=lambda: 200.0,
            ) as directory,
        ):
            retained_b = Path(directory)
            live_file = retained_b / "track-microphone.wav"

            def unstopped_write():
                live_file.write_bytes(b"pcm")
                stuck_entered.set()
                assert release_stuck.wait(5)
                assert live_file.read_bytes() == b"pcm"
                stuck_done.set()

            stuck_executor.submit(unstopped_write)
            assert stuck_entered.wait(5)
            raise ValueError("scenario assertion failed")

        assert not stuck_done.is_set()
        assert retained_b is not None and (retained_b / "track-microphone.wav").is_file()
        sub_b = group_b.value.exceptions
        assert len(sub_b) == 3
        assert isinstance(sub_b[0], ValueError) and "scenario assertion failed" in str(sub_b[0])
        assert isinstance(sub_b[1], AssertionError) and "capture finalization" in str(sub_b[1])
        assert isinstance(sub_b[2], TimeoutError)
        assert "recorder(pipeline=True" in str(sub_b[2])
        assert "library_recovery.executor" in str(sub_b[2])
    finally:
        release_stuck.set()
        assert stuck_done.wait(5)
        stuck_executor.shutdown(wait=True)
        if retained_b is not None and retained_b.exists():
            shutil.rmtree(retained_b)


def test_virtual_microphone_cli_and_tone_power_contracts(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    vm = _load_recording_module("virtual-microphone.py", "virtual_microphone")

    _parser, defaults = vm.parse_args(["produce"])
    assert (defaults.mode, defaults.sink, defaults.hz, defaults.channels) == (
        "produce",
        "cassini_microphone",
        660,
        1,
    )

    _parser, desktop = vm.parse_args(["probe", "--sink", "cassini_desktop", "--hz", "440", "--channels", "2"])
    assert (desktop.mode, desktop.sink, desktop.hz, desktop.channels) == ("probe", "cassini_desktop", 440, 2)

    for bad_argv in (
        ["produce", "--sink", "default"],
        ["produce", "--channels", "3"],
        ["probe", "--hz", "20"],
    ):
        with pytest.raises(SystemExit):
            vm.parse_args(bad_argv)

    def make_pcm(hz):
        samples = [int(0.2 * 32767 * math.sin(math.tau * hz * i / vm.RATE)) for i in range(vm.WINDOW)]
        return struct.pack(f"<{vm.WINDOW}h", *samples)

    pcm_660 = make_pcm(660)
    pcm_440 = make_pcm(440)
    assert vm.tone_power(pcm_660, hz=660) > vm.MIN_POWER * 5
    assert vm.tone_power(pcm_660, hz=440) < vm.MIN_POWER / 10
    assert vm.tone_power(pcm_440, hz=440) > vm.MIN_POWER * 5
    assert vm.tone_power(pcm_440, hz=660) < vm.MIN_POWER / 10

    # Both producer and readiness probe must disable Pulse ringbuffer clock provision so a
    # corked PAUSED->PLAYING preroll race on pulsesink sync=false never freezes audiotestsrc.
    prod_desktop = vm.produce_pipeline(sink="cassini_desktop", hz=1210, channels=2)
    assert "pulsesink device=cassini_desktop sync=false provide-clock=false" in prod_desktop
    assert "rate=48000,channels=2" in prod_desktop
    probe_desktop = vm.probe_pipeline(sink="cassini_desktop")
    assert "pulsesrc device=cassini_desktop.monitor provide-clock=false" in probe_desktop


def test_every_js_recording_suite_probes_virtual_tones_instead_of_inline_audiotestsrc():
    for name in ("app-control.mjs", "call-signals.mjs", "live-folder.mjs", "source-changes.mjs"):
        source = (ROOT / "tests/recording" / name).read_text()
        assert "audiotestsrc" not in source, f"{name} must use virtual-microphone.py instead of inline audiotestsrc"
        assert "virtual-microphone.py" in source
        assert "'probe'" in source
