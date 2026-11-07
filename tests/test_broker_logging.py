# SPDX-License-Identifier: GPL-3.0-or-later
"""Child stderr reaches a journal fd or a bounded private log independently of GUI."""

import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from cassini_talk import broker_logging

BOUND = 10


def environment(tmp_path):
    return dict(
        os.environ,
        HOME=str(tmp_path),
        XDG_STATE_HOME=str(tmp_path / "state"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
    )


def test_raw_stderr_before_application_import_reaches_the_opened_journal_stream(tmp_path, monkeypatch):
    path = tmp_path / "journal-stream"
    monkeypatch.setattr(broker_logging, "journal_stream", lambda: os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    process = broker_logging.launch(
        [sys.executable, "-c", "import os;os.write(2,b'Generated startup diagnostic\\n')"], env=environment(tmp_path)
    )
    assert process.wait(BOUND) == 0
    assert path.read_text() == "Generated startup diagnostic\n"


def test_startup_failure_is_retained_without_journald(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_logging, "journal_stream", lambda: None)
    process = broker_logging.launch(
        [sys.executable, "-c", "raise RuntimeError('Generated startup failure')"], env=environment(tmp_path)
    )
    assert process.wait(BOUND) == 1
    path = tmp_path / "state/cassini/browser-service.log"
    assert "Generated startup failure" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_fallback_rotates_raw_stderr_and_keeps_the_final_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_logging, "journal_stream", lambda: None)
    code = "import os;[os.write(2,b'Generated diagnostic '+b'x'*4000+b'\\n') for _ in range(2200)];os.write(2,b'FINAL diagnostic\\n')"
    process = broker_logging.launch([sys.executable, "-c", code], env=environment(tmp_path))
    assert process.wait(BOUND) == 0
    directory = tmp_path / "state/cassini"
    paths = [directory / name for name in ("browser-service.log", "browser-service.log.1", "browser-service.log.2")]
    assert sorted(p.name for p in directory.iterdir()) == sorted([p.name for p in paths] + ["browser-service.log.lock"])
    assert stat.S_IMODE((directory / "browser-service.log.lock").stat().st_mode) == 0o600
    assert len(paths) == 3
    assert sum(path.stat().st_size for path in paths) <= (broker_logging.MAX_BYTES + 8192) * 3
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    assert "FINAL diagnostic" in (tmp_path / "state/cassini/browser-service.log").read_text()


def test_real_broker_warning_reaches_the_persistent_fallback_sink(tmp_path, monkeypatch):
    from test_broker_identity import close_broker

    from cassini_talk import browser_service

    env = environment(tmp_path)
    for name in ("HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, env[name])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setattr(broker_logging, "journal_stream", lambda: None)
    launched, launch = [], broker_logging.launch

    def observed(*args, **kwargs):
        process = launch(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(broker_logging, "launch", observed)
    path = tmp_path / "b.sock"
    try:
        browser_service.ensure_service(path)
        with browser_service.connect(path, timeout=2) as connection, connection.makefile("rwb") as stream:
            browser_service.send_packet(stream, dict(role="extension", browserName="Generated browser"))
            browser_service.send_packet(stream, dict(type="hello", version=2))
            assert browser_service.read_packet(stream) is None
    finally:
        close_broker(path)
        for process in launched:
            assert process.wait(BOUND) == 0
    diagnostic = (tmp_path / "state/cassini/browser-service.log").read_text()
    assert "Browser service started:" in diagnostic
    assert "Browser companion speaks control version 2" in diagnostic


def test_diagnostic_log_does_not_follow_a_symlink(tmp_path):
    import pytest

    target = tmp_path / "kept-data"
    target.write_text("unchanged")
    path = tmp_path / "browser-service.log"
    path.symlink_to(target)
    with pytest.raises(OSError):
        broker_logging._open_log(path)
    assert target.read_text() == "unchanged"


def _rotation_writer(path, message, ready, go, rotating=None, release=None):
    import logging

    handler = broker_logging.PrivateLog(path, maxBytes=256, backupCount=2, encoding="utf-8")
    if rotating is not None:
        rollover = handler.doRollover

        def held_rollover():
            rotating.set()
            assert release.wait(BOUND), "parent did not release the held rotation"
            rollover()

        handler.doRollover = held_rollover
    try:
        ready.set()
        assert go.wait(BOUND), "parent did not release the writer"
        handler.emit(logging.LogRecord("generated", logging.INFO, "", 0, message, (), None))
    finally:
        handler.close()


def test_processes_serialize_rotation_and_refresh_a_replaced_log_inode(tmp_path):
    import fcntl
    import multiprocessing

    import pytest

    context = multiprocessing.get_context("spawn")
    path = tmp_path / "browser-service.log"
    seed = "seed\n" * 36
    path.write_text(seed)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    # Keep the second process's original data fd open across the first rotation.
    ready_a, ready_b, go_a, go_b, rotating, release = [context.Event() for _ in range(6)]
    first = context.Process(target=_rotation_writer, args=(path, "A" * 100, ready_a, go_a, rotating, release))
    second = context.Process(target=_rotation_writer, args=(path, "B" * 40, ready_b, go_b))
    first.start()
    second.start()
    try:
        assert ready_a.wait(BOUND) and ready_b.wait(BOUND)
        go_a.set()
        assert rotating.wait(BOUND)
        # This is a kernel-lock assertion, not a sleep asserting non-occurrence:
        # the lock must cover the entire rotation, including writing the record.
        with (tmp_path / "browser-service.log.lock").open("r+") as lock, pytest.raises(BlockingIOError):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        go_b.set()
        release.set()
        for process in (first, second):
            process.join(BOUND)
            assert not process.is_alive() and process.exitcode == 0
        assert path.read_text().splitlines() == ["A" * 100, "B" * 40]
        assert path.with_suffix(".log.1").read_text() == seed
        assert not path.with_suffix(".log.2").exists()
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in tmp_path.iterdir())
    finally:
        release.set()
        go_a.set()
        go_b.set()
        for process in (first, second):
            process.join(BOUND)
            if process.is_alive():
                process.terminate()
                process.join(BOUND)


def test_diagnostic_lock_does_not_follow_a_symlink(tmp_path):
    import pytest

    target = tmp_path / "kept-data"
    target.write_text("unchanged")
    path = tmp_path / "browser-service.log"
    path.with_suffix(".log.lock").symlink_to(target)
    with pytest.raises(OSError):
        broker_logging.PrivateLog(path, maxBytes=256, backupCount=2)
    assert target.read_text() == "unchanged"


def test_fifo_at_log_path_cannot_block_the_launcher(tmp_path):
    import pytest

    path = tmp_path / "browser-service.log"
    os.mkfifo(path)
    with pytest.raises(OSError):
        broker_logging._open_log(path)


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_collector_forwards_shutdown_and_real_broker_removes_its_socket(tmp_path, monkeypatch, number):
    from test_broker_identity import close_broker

    from cassini_talk import browser_service

    env = environment(tmp_path)
    for name in ("HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, env[name])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setattr(broker_logging, "journal_stream", lambda: None)
    launched, launch = [], broker_logging.launch

    def observed(*args, **kwargs):
        process = launch(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(broker_logging, "launch", observed)
    path = tmp_path / "b.sock"
    try:
        browser_service.ensure_service(path)
        status = browser_service.request("status", path=path)
        (collector,) = launched
        assert status["identity"]["pid"] != collector.pid
        collector.send_signal(number)
        assert collector.wait(BOUND) == 0
        assert not path.exists(), "broker shutdown must finish its socket cleanup"
    finally:
        close_broker(path)
        for process in launched:
            if process.poll() is None:
                process.terminate()
                process.wait(BOUND)


def test_fallback_collects_stderr_after_its_gui_launcher_has_exited(tmp_path):
    import select
    import subprocess

    ready, go = tmp_path / "ready", tmp_path / "go"
    os.mkfifo(ready)
    os.mkfifo(go)
    # Hold both ends open so FIFO open/readiness never depends on a sleep.
    ready_fd, go_fd = [os.open(p, os.O_RDWR | os.O_NONBLOCK) for p in (ready, go)]
    pid_fd = None
    child = (
        f"with open({str(ready)!r}, 'wb', buffering=0) as out: out.write(b'R')\n"
        f"with open({str(go)!r}, 'rb', buffering=0) as gate: gate.read(1)\n"
        "import os\nos.write(2,b'Generated diagnostic AFTER GUI exit\\n')\n"
    )
    launcher = (
        "import os,sys\nfrom cassini_talk import broker_logging\n"
        "broker_logging.journal_stream=lambda:None\n"
        f"print(broker_logging.launch([sys.executable,'-c',{child!r}],env=dict(os.environ)).pid)\n"
    )
    try:
        gui = subprocess.run(
            [sys.executable, "-c", launcher], env=environment(tmp_path), capture_output=True, text=True, timeout=BOUND
        )
        assert gui.returncode == 0, gui.stderr
        pid_fd = os.pidfd_open(int(gui.stdout.strip()))
        assert select.select([ready_fd], [], [], BOUND)[0]
        assert os.read(ready_fd, 1) == b"R"
        # Only after the simulated GUI exits do we release the child's stderr.
        os.write(go_fd, b"G")
        assert select.select([pid_fd], [], [], BOUND)[0], "collector did not drain and exit"
        path = tmp_path / "state/cassini/browser-service.log"
        assert "Generated diagnostic AFTER GUI exit" in path.read_text()
    finally:
        os.close(ready_fd)
        os.close(go_fd)
        if pid_fd is not None:
            if not select.select([pid_fd], [], [], 0)[0]:
                signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
                assert select.select([pid_fd], [], [], BOUND)[0]
            os.close(pid_fd)


def test_rotation_budget_counts_utf8_bytes_instead_of_characters(tmp_path):
    import logging

    path = tmp_path / "browser-service.log"
    handler = broker_logging.PrivateLog(path, maxBytes=256, backupCount=2, encoding="utf-8")
    try:
        for message in ("A" * 200, "漢" * 30):
            handler.emit(logging.LogRecord("generated", logging.INFO, "", 0, message, (), None))
    finally:
        handler.close()
    assert path.read_text() == "漢" * 30 + "\n"
    assert path.with_suffix(".log.1").read_text() == "A" * 200 + "\n"
    assert path.stat().st_size <= 256 and path.with_suffix(".log.1").stat().st_size <= 256


def test_pytest_defaults_keep_broker_state_private_and_never_probe_the_journal(tmp_path, monkeypatch):
    assert os.environ["XDG_STATE_HOME"] == str(tmp_path / "state")
    assert broker_logging.journal_stream() is None

    def forbidden_journal(*_args, **_kwargs):
        pytest.fail("unit tests must not open the caller's system journal")

    monkeypatch.setattr(broker_logging.ctypes, "CDLL", forbidden_journal)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = broker_logging.launch(
        [sys.executable, "-c", "import os;os.write(2,b'Private pytest diagnostic\\n')"], env=env
    )
    assert process.wait(BOUND) == 0
    assert "Private pytest diagnostic" in (tmp_path / "state/cassini/browser-service.log").read_text()
