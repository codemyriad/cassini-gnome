# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic teardown and fixture-probe contracts for recording tests."""

import importlib.util
import math
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

    app = SimpleNamespace(
        _capture_exiting=False,
        _capture_background_saving=False,
        recorder=None,
        get_windows=lambda: [SimpleNamespace(close=lambda: events.append("window_close"))],
        library_recovery=SimpleNamespace(cancelled=threading.Event(), executor=recovery_executor),
    )

    with (
        pytest.raises(RuntimeError, match="synthetic scenario failure"),
        tempfile.TemporaryDirectory() as directory,
        teardown.recording_app_teardown(
            app,
            on_error=lambda: events.append(("on_error", Path(directory).is_dir())),
            stop_active=lambda: (events.append("stop_active"), release_worker.set()),
            timeout=5,
        ),
    ):
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
    assert not Path(directory).exists()


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
