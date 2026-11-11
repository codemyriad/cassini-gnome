# SPDX-License-Identifier: GPL-3.0-or-later
"""Private processes/sockets: identity, guarded idle migration and graceful exit."""

import base64
import contextlib
import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cassini_talk import browser_service
from cassini_talk.browser_audio_proxy import BrowserAudioProxy

ROOT = Path(__file__).resolve().parents[1]
BOUND = 8


@pytest.fixture
def private(tmp_path, monkeypatch):
    for key, value in dict(
        HOME=tmp_path / "home",
        XDG_CONFIG_HOME=tmp_path / "config",
        XDG_DATA_HOME=tmp_path / "data",
        XDG_STATE_HOME=tmp_path / "state",
        XDG_RUNTIME_DIR=tmp_path / "run",
        CASSINI_LIBRARY_DIR=tmp_path / "Library",
    ).items():
        monkeypatch.setenv(key, str(value))
    from cassini_talk import broker_logging

    monkeypatch.setattr(broker_logging, "journal_stream", lambda: None)
    return tmp_path / "b.sock"


def readable(stream):
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        assert selector.select(BOUND), "Broker process did not become ready"


@contextlib.contextmanager
def legacy(path, module="cassini_viewer", *, active=False):
    package = path.parent / module
    package.mkdir()
    (package / "__init__.py").touch()
    shutil.copyfile(ROOT / "tests/fixtures/legacy_browser_service.py", package / "browser_service.py")
    if active:
        path.with_suffix(".busy").touch()
    process = subprocess.Popen(
        [sys.executable, "-m", module + ".browser_service", "--serve", str(path)],
        env=dict(os.environ, PYTHONPATH=str(path.parent), CASSINI_LEGACY_TEST="1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        readable(process.stdout)
        assert process.stdout.readline().strip() == "READY"
        yield process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        process.communicate(timeout=BOUND)


def close_broker(path):
    try:
        status = browser_service.request(path=path)
    except browser_service.BROKER_ABSENT:
        return
    if isinstance(status.get("identity"), dict):
        descriptor = os.pidfd_open(status["identity"]["pid"])
        try:
            result = browser_service.request("shutdown", path=path, identity=status["identity"])
            assert result["shutdown"]
            readable(descriptor)
        finally:
            os.close(descriptor)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(status["identity"]["pid"], os.WNOHANG)


def test_matching_broker_is_reused_and_audio_ready_carries_its_identity(private):
    from cassini_talk.broker_identity import matches

    try:
        browser_service.ensure_service(private)
        first = browser_service.request(path=private)
        assert matches(first["identity"]) and first["brokerStale"] is False
        assert first["identity"]["pid"] != os.getpid()
        assert first["identity"]["startedAt"] <= time.time_ns() // 1_000_000
        browser_service.ensure_service(private)
        assert browser_service.request(path=private)["identity"] == first["identity"]
        with contextlib.closing(BrowserAudioProxy(path=private)) as proxy:
            assert proxy.identity == first["identity"] and not proxy.legacy and not proxy.broker_stale
    finally:
        close_broker(private)


def test_idle_legacy_broker_retires_before_current_broker_starts(private):
    with legacy(private) as old:
        try:
            browser_service.ensure_service(private)
            assert old.wait(BOUND) == 0
            status = browser_service.request(path=private)
            assert status["identity"]["pid"] != old.pid and not status["brokerStale"]
        finally:
            close_broker(private)


class _ControlledClock:
    def __init__(self, initial=100.0):
        self._lock = threading.Lock()
        self._now = float(initial)

    def __call__(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        with self._lock:
            self._now += float(seconds)


class _ObservedSocket:
    def __init__(self, inner, *, track_final=False):
        self._inner = inner
        self._track_final = track_final
        self.entering_recv = threading.Event()
        self.eof_seen = False
        self.closed = False

    def recv(self, *args, **kwargs):
        if self._track_final:
            self.entering_recv.set()
        data = self._inner.recv(*args, **kwargs)
        if self._track_final and data == b"":
            self.eof_seen = True
        return data

    def close(self):
        self.closed = True
        return self._inner.close()

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *args):
        self.closed = True
        return self._inner.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextlib.contextmanager
def _gated_legacy_retirement(private, monkeypatch, *, ignore_sigint=False, gate_unwinding_flush=False):
    import fcntl
    from types import SimpleNamespace

    from cassini_talk import broker_lifecycle

    in_flight_r, in_flight_w = os.pipe()
    release_r, release_w = os.pipe()
    os.set_inheritable(in_flight_w, True)
    os.set_inheritable(release_r, True)
    (private.parent / "sitecustomize.py").write_text(
        "import os, signal, socket\n"
        "if os.environ.get('CASSINI_IGNORE_RETIRE_SIGINT') == '1':\n"
        "    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "if os.environ.get('CASSINI_GATE_SECOND_FLUSH') == '1':\n"
        "    _orig = socket.SocketIO.write\n"
        "    _calls = 0\n"
        "    def _gated_write(self, b):\n"
        "        global _calls\n"
        "        _calls += 1\n"
        "        if _calls == 2:\n"
        "            sent = self._sock.send(b)\n"
        "            os.write(int(os.environ['CASSINI_FLUSH_OUT_FD']), b'1')\n"
        "            os.read(int(os.environ['CASSINI_FLUSH_IN_FD']), 1)\n"
        "            return sent\n"
        "        if _calls == 3 and os.environ.get('CASSINI_GATE_UNWINDING_FLUSH') == '1':\n"
        "            os.write(int(os.environ['CASSINI_FLUSH_OUT_FD']), b'2')\n"
        "            os.read(int(os.environ['CASSINI_FLUSH_IN_FD']), 1)\n"
        "            return _orig(self, b)\n"
        "        return _orig(self, b)\n"
        "    socket.SocketIO.write = _gated_write\n",
        encoding="utf-8",
    )

    state = SimpleNamespace(
        clock=_ControlledClock(),
        final_socket=None,
        final_socket_ready=threading.Event(),
        sigint_state=None,
        released_closed_states=[],
        lock_busy_observed=threading.Event(),
        closed_at_lock_busy=None,
        cancel_event=None,
        lock_busy_after_cancel=threading.Event(),
        closed_after_cancel=None,
        on_lock_busy_after_cancel=None,
    )
    connect_calls = 0
    real_connect = browser_service.connect
    real_inspect = broker_lifecycle.inspect
    real_released = broker_lifecycle._released
    real_send_signal = signal.pidfd_send_signal
    real_flock = fcntl.flock

    def spy_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        is_final = connect_calls == 2
        wrapped = _ObservedSocket(real_connect(*args, **kwargs), track_final=is_final)
        if is_final:
            state.final_socket = wrapped
            state.final_socket_ready.set()
        return wrapped

    def spy_inspect(connection):
        raw = connection._inner if isinstance(connection, _ObservedSocket) else connection
        return real_inspect(raw)

    def spy_send_signal(pidfd, sig, *args):
        assert state.final_socket is not None
        state.sigint_state = (state.final_socket.eof_seen, state.final_socket.closed)
        return real_send_signal(pidfd, sig, *args)

    def spy_released(path, cancelled, *, clock=None):
        before = state.final_socket.closed if state.final_socket is not None else None
        try:
            if clock is None:
                return real_released(path, cancelled)
            return real_released(path, cancelled, clock=clock)
        finally:
            after = state.final_socket.closed if state.final_socket is not None else None
            state.released_closed_states.append((before, after))

    def spy_flock(fd, operation):
        try:
            return real_flock(fd, operation)
        except BlockingIOError:
            if state.final_socket is not None:
                if not state.lock_busy_observed.is_set():
                    state.closed_at_lock_busy = state.final_socket.closed
                    state.lock_busy_observed.set()
                if (
                    state.cancel_event is not None
                    and state.cancel_event.is_set()
                    and not state.lock_busy_after_cancel.is_set()
                ):
                    state.closed_after_cancel = state.final_socket.closed
                    state.lock_busy_after_cancel.set()
                    if state.on_lock_busy_after_cancel is not None:
                        state.on_lock_busy_after_cancel()
            raise

    monkeypatch.setattr(browser_service, "connect", spy_connect)
    monkeypatch.setattr(broker_lifecycle, "inspect", spy_inspect)
    monkeypatch.setattr(broker_lifecycle, "_released", spy_released)
    monkeypatch.setattr(signal, "pidfd_send_signal", spy_send_signal)
    monkeypatch.setattr(fcntl, "flock", spy_flock)

    package = private.parent / "cassini_viewer"
    package.mkdir()
    (package / "__init__.py").touch()
    shutil.copyfile(ROOT / "tests/fixtures/legacy_browser_service.py", package / "browser_service.py")
    env = dict(
        os.environ,
        PYTHONPATH=str(private.parent),
        CASSINI_LEGACY_TEST="1",
        CASSINI_GATE_SECOND_FLUSH="1",
        CASSINI_GATE_UNWINDING_FLUSH="1" if gate_unwinding_flush else "0",
        CASSINI_IGNORE_RETIRE_SIGINT="1" if ignore_sigint else "0",
        CASSINI_FLUSH_OUT_FD=str(in_flight_w),
        CASSINI_FLUSH_IN_FD=str(release_r),
    )
    old = subprocess.Popen(
        [sys.executable, "-m", "cassini_viewer.browser_service", "--serve", str(private)],
        env=env,
        pass_fds=(in_flight_w, release_r),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(in_flight_w)
    os.close(release_r)
    try:
        readable(old.stdout)
        assert old.stdout.readline().strip() == "READY"
        yield old, in_flight_r, release_w, state
    finally:
        for fd in (in_flight_r, release_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        if old.poll() is None:
            old.kill()
            old.communicate(timeout=BOUND)
        close_broker(private)


def test_legacy_broker_status_flush_completes_before_retirement_sigint(private, monkeypatch):
    """Verify SIGINT follows actual peer EOF on a frozen clock while keeping `again` open across retirement."""
    with _gated_legacy_retirement(private, monkeypatch) as (old, in_flight_r, release_w, state):
        with ThreadPoolExecutor(1) as executor:
            ensuring = executor.submit(browser_service.ensure_service, private, clock=state.clock)
            try:
                readable(in_flight_r)
                assert os.read(in_flight_r, 1) == b"1"
                assert state.final_socket_ready.wait(BOUND)
                assert state.final_socket.entering_recv.wait(BOUND)
                assert state.sigint_state is None
                assert not state.final_socket.eof_seen and not state.final_socket.closed
            finally:
                with contextlib.suppress(OSError):
                    os.write(release_w, b"1")
            ensuring.result(BOUND)
        rc = old.wait(BOUND)
        _stdout, stderr = old.communicate(timeout=BOUND)
        assert state.sigint_state == (True, False), f"Expected (eof_seen=True, closed=False) at SIGINT:\n{stderr}"
        assert state.released_closed_states == [(False, False), (True, True)]
        assert state.final_socket.closed
        assert rc == 0, f"Legacy broker exited with {rc}:\n{stderr}"
        status = browser_service.request(path=private)
        assert status["identity"]["pid"] != old.pid and not status["brokerStale"]


def test_legacy_broker_eof_timeout_keeps_status_socket_open_through_retirement(private, monkeypatch):
    """Expire the 0.5 s EOF bound while the legacy flush is gated; `again` stays open across `_released`."""
    with _gated_legacy_retirement(private, monkeypatch) as (old, in_flight_r, _release_w, state):
        with ThreadPoolExecutor(1) as executor:
            ensuring = executor.submit(browser_service.ensure_service, private, clock=state.clock)
            readable(in_flight_r)
            assert os.read(in_flight_r, 1) == b"1"
            assert state.final_socket_ready.wait(BOUND)
            assert state.final_socket.entering_recv.wait(BOUND)
            assert state.sigint_state is None and not state.final_socket.eof_seen
            # Advance the injected clock past `_await_peer_close`'s 0.5 s bound without releasing the pipe;
            # SIGINT interrupts `os.read` inside `SocketIO.write`, and the unwinding flush succeeds because
            # `again` remains open until `_released` observes `.lock` release.
            state.clock.advance(0.6)
            ensuring.result(BOUND)
        rc = old.wait(BOUND)
        _stdout, stderr = old.communicate(timeout=BOUND)
        assert state.sigint_state == (False, False), f"Expected (eof_seen=False, closed=False) on timeout:\n{stderr}"
        assert state.released_closed_states == [(False, False), (True, True)]
        assert state.final_socket.closed
        assert rc == 0, f"Legacy broker exited with {rc} on EOF timeout path:\n{stderr}"
        status = browser_service.request(path=private)
        assert status["identity"]["pid"] != old.pid and not status["brokerStale"]


def test_legacy_broker_retirement_lock_timeout_escalates_and_closes_status_socket(private, monkeypatch):
    """Escalate with TimeoutError and close `again` when a wedged legacy broker never releases `.lock`."""
    with _gated_legacy_retirement(private, monkeypatch, ignore_sigint=True) as (_old, in_flight_r, release_w, state):
        with ThreadPoolExecutor(1) as executor:
            ensuring = executor.submit(browser_service.ensure_service, private, clock=state.clock)
            readable(in_flight_r)
            assert os.read(in_flight_r, 1) == b"1"
            assert state.final_socket_ready.wait(BOUND)
            assert state.final_socket.entering_recv.wait(BOUND)
            state.clock.advance(0.6)
            assert state.lock_busy_observed.wait(BOUND)
            assert state.sigint_state == (False, False)
            assert state.closed_at_lock_busy is False
            state.clock.advance(5.1)
            with pytest.raises(TimeoutError, match="The older browser service has not finished closing"):
                ensuring.result(BOUND)
        assert state.released_closed_states == [(False, False)]
        assert state.final_socket.closed
        os.write(release_w, b"1")


def test_legacy_broker_cancellation_after_sigint_defers_until_unwinding_flush_completes(private, monkeypatch):
    """Defer cancellation after SIGINT until the unwinding flush and `.lock` release finish, then raise CancelledError."""
    from concurrent.futures import CancelledError

    cancelled = threading.Event()
    post_cancel_observed = threading.Event()
    with _gated_legacy_retirement(private, monkeypatch, gate_unwinding_flush=True) as (
        old,
        in_flight_r,
        release_w,
        state,
    ):
        state.cancel_event = cancelled
        state.on_lock_busy_after_cancel = post_cancel_observed.set
        with ThreadPoolExecutor(1) as executor:
            ensuring = executor.submit(browser_service.ensure_service, private, cancelled=cancelled, clock=state.clock)
            ensuring.add_done_callback(lambda _future: post_cancel_observed.set())
            try:
                readable(in_flight_r)
                assert os.read(in_flight_r, 1) == b"1"
                assert state.final_socket_ready.wait(BOUND)
                assert state.final_socket.entering_recv.wait(BOUND)
                state.clock.advance(0.6)
                # Wait for SIGINT to interrupt the initial flush and park inside the unwinding flush
                # (`_calls == 3`), and for `_released` to observe `.lock` held by the retiring broker.
                readable(in_flight_r)
                assert os.read(in_flight_r, 1) == b"2"
                assert state.lock_busy_observed.wait(BOUND)
                assert state.sigint_state == (False, False)
                assert state.closed_at_lock_busy is False
                # Simulate BrowserWatch closing after SIGINT while the unwinding flush remains pending.
                cancelled.set()
                assert post_cancel_observed.wait(BOUND)
                assert state.lock_busy_after_cancel.is_set() and not ensuring.done()
                assert state.closed_after_cancel is False and not state.final_socket.closed
            finally:
                with contextlib.suppress(OSError):
                    os.write(release_w, b"2")
            with pytest.raises(CancelledError):
                ensuring.result(BOUND)
        rc = old.wait(BOUND)
        _stdout, stderr = old.communicate(timeout=BOUND)
        assert state.released_closed_states == [(False, False)]
        assert state.final_socket.closed
        assert rc == 0, f"Legacy broker exited with {rc} when cancelled during unwinding flush:\n{stderr}"
        assert not private.exists(), "Cancelled ensure_service must not spawn a replacement broker"


@pytest.mark.parametrize("module,active", [("cassini_viewer", True), ("unrelated_service", False)])
def test_recording_legacy_or_unrelated_socket_owner_is_never_signalled(private, module, active):
    with legacy(private, module, active=active) as old:
        browser_service.ensure_service(private)
        assert old.poll() is None
        status = browser_service.request(path=private)
        assert status["brokerStale"] and status["statuses"][0]["recording"] is active


def test_newer_idle_protocol_retires_a_stale_payload_without_signals(private, monkeypatch):
    from test_browser_service import stop_broker

    service = browser_service.BrowserService(private.with_suffix(".json"), notifier=lambda *_: None)
    service.identity["buildId"] = "old-build"
    path, ready = private, threading.Event()
    thread = threading.Thread(target=service.serve, args=(path, ready), daemon=True)
    thread.start()
    assert ready.wait(BOUND)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *_: pytest.fail("Signalled a shutdown-capable broker"))
    try:
        browser_service.ensure_service(path)
        thread.join(BOUND)
        assert not thread.is_alive() and service.closed.is_set()
        assert not browser_service.request(path=path)["brokerStale"]
    finally:
        close_broker(path)
        stop_broker(service, thread, path)


def test_shutdown_rechecks_identity_and_active_capture_under_the_broker_lock(private):
    from types import SimpleNamespace

    service = browser_service.BrowserService(private.with_suffix(".json"), notifier=lambda *_: None)
    try:
        assert service.shutdown_idle(dict(service.identity, pid=-1)) == dict(shutdown=False, reason="changed")
        service.captures["opening"] = SimpleNamespace(finished=False)
        assert service.shutdown_idle(service.identity) == dict(shutdown=False, reason="busy")
        assert not service.closed.is_set()
        service.captures.clear()
        assert service.shutdown_idle(service.identity) == dict(shutdown=True)
        with pytest.raises(ValueError, match="cancelled"):
            service.start_all()
    finally:
        service.workers.shutdown(wait=True)


def test_sigterm_finalizes_capture_and_removes_the_socket(private):
    control = stream = None
    try:
        browser_service.ensure_service(private)
        identity = browser_service.request(path=private)["identity"]
        control = browser_service.connect(private)
        stream = control.makefile("rwb")
        browser_service.send_packet(stream, dict(role="extension"))
        hello = dict(type="hello", version=1, capabilities=["independent-sessions-v1", "call-signals-v1"])
        browser_service.send_packet(stream, hello)
        assert browser_service.read_packet(stream)["type"] == "configure"
        browser_service.send_packet(
            stream,
            dict(
                type="meetings",
                meetings=[
                    dict(
                        tabId=2,
                        title="Synthetic manual writer",
                        url="https://fixture.invalid/manual",
                        sourceCount=1,
                        pageState="present",
                    )
                ],
            ),
        )
        browser_service.send_packet(stream, dict(type="status", recording=False))
        # A second configure reply is an ordered barrier after the inventory.
        browser_service.send_packet(stream, hello)
        assert browser_service.read_packet(stream)["type"] == "configure"
        with ThreadPoolExecutor(1) as executor:
            asking = executor.submit(browser_service.request, "start-all", path=private, microphone=False)
            command = browser_service.read_packet(stream)
            assert command["action"] == "start"
            browser_service.send_packet(
                stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False))
            )
            assert asking.result(BOUND)["attempted"] == 1
        proxy = BrowserAudioProxy(path=private)
        try:
            result = proxy.handle(
                dict(
                    type="start",
                    version=1,
                    independent=True,
                    captureId="generated",
                    requestId=command["requestId"],
                    timelineOriginMs=time.time_ns() // 1_000_000,
                    microphone=False,
                )
            )
            directory = Path(result["path"])
            proxy.handle(dict(type="track", id="wire", kind="remote", label="Generated", sampleRate=48000, channels=1))
            proxy.handle(
                dict(type="audio", id="wire", sequence=0, offsetMs=0, pcm=base64.b64encode(bytes(960)).decode())
            )
            descriptor = os.pidfd_open(identity["pid"])
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                readable(descriptor)
            finally:
                os.close(descriptor)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(identity["pid"], 0)
            assert not private.exists()
            saved = json.loads((directory / "session.json").read_text())
            assert saved["problem"] == "Recording service closed" and saved["tracks"][0]["frames"] == 480
        finally:
            proxy.close()
    finally:
        if stream is not None:
            stream.close()
        if control is not None:
            control.close()
        close_broker(private)


@pytest.mark.parametrize("with_identity", [False, True])
def test_audio_proxy_accepts_legacy_ready_and_marks_its_identity(private, with_identity):
    ready = threading.Event()
    errors = []
    identity = dict(app="cassini-talk", version="older", buildId="older", moduleRoot="/older")

    def serve():
        try:
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(private))
                server.listen()
                ready.set()
                connection, _ = server.accept()
                with connection, connection.makefile("rwb") as stream:
                    assert browser_service.read_packet(stream) == dict(role="audio")
                    packet = dict(ready=True, protocol=2)
                    if with_identity:
                        packet["identity"] = identity
                    browser_service.send_packet(stream, packet)
                    assert stream.read() == b""
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(BOUND)
    try:
        with contextlib.closing(BrowserAudioProxy(path=private)) as proxy:
            assert proxy.identity == (identity if with_identity else None)
            assert proxy.legacy is (not with_identity) and proxy.broker_stale
    finally:
        thread.join(BOUND)
    assert not thread.is_alive() and not errors, errors


def test_installed_identity_reads_the_generated_manifest_and_source_patch_changes_identity(tmp_path):
    from test_installed_build import payload

    from cassini_talk.broker_identity import expected

    value, root, _launcher = payload(tmp_path)
    assert expected(root)["buildId"] == value["buildId"] and expected(root)["version"] == value["version"]
    (root / "build-info.json").unlink()
    before = expected(root)
    (root / "cassini_talk/example.py").write_text("different source")
    assert expected(root)["buildId"] != before["buildId"]


def test_old_flatpak_client_targets_current_host_but_reports_its_own_loaded_build(monkeypatch):
    from cassini_talk import broker_identity, installed_build

    monkeypatch.setattr(broker_identity, "_host_targets", {})
    loaded = broker_identity.expected()
    current = dict(version="next", buildId="f" * 64)
    monkeypatch.setenv("FLATPAK_ID", "io.codemyriad.CassiniTalk")
    monkeypatch.setattr(installed_build, "host_identity", lambda: (current, Path("/new-deployment")))
    assert broker_identity.running()["buildId"] == loaded["buildId"]
    expected = dict(loaded, **current)
    assert broker_identity.target() == expected
    assert broker_identity.matches(expected)
    assert not broker_identity.matches(loaded)


@pytest.mark.parametrize("activity", ["group", "pending", "scheduled", "stopping"])
def test_idle_shutdown_waits_for_inflight_recording_operations(private, activity):
    service = browser_service.BrowserService(private.with_suffix(".json"), notifier=lambda *_: None)
    try:
        if activity == "group":
            service._record_groups["generated"] = threading.Event()
        elif activity == "pending":
            service._pending_calls["generated"] = object()
        elif activity == "scheduled":
            service._scheduled_calls.add("generated")
        else:
            service._stopping_all.set()
        assert service.shutdown_idle(service.identity) == dict(shutdown=False, reason="busy")
        assert not service.closed.is_set()
    finally:
        service.workers.shutdown(wait=True)
