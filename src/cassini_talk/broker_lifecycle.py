# SPDX-License-Identifier: GPL-3.0-or-later
"""Retire only an identified idle broker, off GTK and without touching captures."""

import contextlib
import fcntl
import logging
import os
import re
import signal
import socket
import stat
import struct
import threading
import time
from concurrent.futures import CancelledError
from pathlib import Path

from . import broker_identity

LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def handover(path, cancelled=None, *, clock=None):
    """Serialize clients across retirement, lock release and replacement readiness."""
    now = clock or time.monotonic
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise PermissionError("Browser service directory must be private to this user")
    descriptor = os.open(
        path.with_suffix(".startup.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        if os.fstat(descriptor).st_uid != os.getuid() or not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PermissionError("Browser startup lock must be an owned regular file")
        deadline = now() + 15
        while True:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if now() >= deadline:
                    raise TimeoutError("Browser service handover is still in progress") from None
                (cancelled or threading.Event()).wait(0.025)
        yield
    finally:
        os.close(descriptor)


def busy(status):
    if status.get("brokerBusy") is True:
        return True
    states, meetings = status.get("statuses"), status.get("meetings", [])
    if not isinstance(states, list) or not isinstance(meetings, list):
        return True  # No evidence that this process is idle.
    return any(
        not isinstance(row, dict)
        or row.get("recording")
        or row.get("stopping")
        or row.get("starting")
        or row.get("busy")
        or row.get("browserRecording")
        or row.get("phase") in ("starting", "recording", "saving")
        for row in [*states, *meetings]
    )


def inspect(connection):
    from .browser_service import read_packet, send_packet

    credentials = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    with connection.makefile("rwb") as stream:
        send_packet(stream, dict(role="app", action="status"))
        value = read_packet(stream)
    if not isinstance(value, dict) or value.get("error"):
        raise OSError("Browser service did not report its status")
    return value, credentials


def _legacy_process(pid, uid, path):
    if uid != os.getuid() or pid == os.getpid():
        return False
    proc = Path("/proc") / str(pid)
    try:
        if proc.stat().st_uid != uid:
            return False
        args = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        executable = (proc / "exe").resolve(strict=True)
        return (
            len(args) == 5
            and re.fullmatch(r"python(?:[23](?:\.\d+)*)?", executable.name) is not None
            and Path(os.fsdecode(args[0])).resolve() == executable
            and args[1] == b"-m"
            and args[2] in (b"cassini_viewer.browser_service", b"cassini_talk.browser_service")
            and args[3] == b"--serve"
            and Path(os.fsdecode(args[4])).resolve() == path.resolve()
        )
    except (OSError, ValueError):
        return False


def _released(path, cancelled, *, clock=None):
    now = clock or time.monotonic
    deadline = now() + 5
    from .installed_build import _open_regular

    try:
        lock = _open_regular(path.with_suffix(".lock"))
    except FileNotFoundError:
        return  # No lock inode exists for a previous listener.
    with lock:
        if os.fstat(lock.fileno()).st_uid != os.getuid():
            raise PermissionError("Browser service lock belongs to another user")
        while True:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if now() >= deadline:
                    raise TimeoutError("The older browser service has not finished closing") from None
                # A filesystem lock has no notification fd. Bound this worker
                # wait, and let a cancelled watcher interrupt it immediately.
                (cancelled or threading.Event()).wait(0.025)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
                return


def _wait_released(path, cancelled, clock):
    if clock is None:
        return _released(path, cancelled)
    return _released(path, cancelled, clock=clock)


def _await_peer_close(connection, cancelled, *, clock=None):
    """Wait boundedly for a one-shot status peer to finish flushing and close its stream.

    Returns True when peer EOF (or socket reset) is observed, False when the 0.5 s bound
    expires with the peer stream still open. Keeping `connection` open across retirement
    signalling and `_released` ensures that even on the timeout path, a legacy broker
    unwinding `with connection.makefile("rwb")` after SIGINT flushes into an open peer
    instead of failing with BrokenPipeError (exit status 1).
    """
    now = clock or time.monotonic
    deadline = now() + 0.5
    while True:
        if cancelled is not None and cancelled.is_set():
            raise CancelledError()
        remaining = deadline - now()
        if remaining <= 0:
            return False
        connection.settimeout(min(0.05, remaining))
        try:
            if not connection.recv(4096):
                return True
        except TimeoutError:
            continue
        except OSError:
            return True


def reusable(connection, path, *, cancelled=None, clock=None):
    """True: retain this listener; False: it retired and released its lock."""
    from .browser_service import connect, request

    try:
        status, (pid, uid, _gid) = inspect(connection)
    except OSError:
        _wait_released(path, cancelled, clock)
        return False
    if busy(status):
        return True  # Active audio never waits for optional installed-build discovery.
    local = broker_identity.target()
    if broker_identity.matches(status.get("identity"), local):
        return True
    if cancelled is not None and cancelled.is_set():
        raise CancelledError()
    with contextlib.ExitStack() as resources:
        signalled = False
        if status.get("idleShutdown") is True:
            try:
                result = request("shutdown", path=path, identity=status.get("identity"))
            except OSError:
                _wait_released(path, cancelled, clock)
                return False
            if result.get("shutdown") is not True:
                return True  # A capture started, or a different broker took over.
        else:
            # A pidfd pins the socket peer's process across /proc checks and the
            # final status read, so a recycled PID can never be signalled.
            try:
                descriptor = os.pidfd_open(pid)
            except (AttributeError, OSError):
                return True
            resources.callback(os.close, descriptor)
            if not _legacy_process(pid, uid, path):
                return True
            try:
                again = connect(path, timeout=0.5)
            except OSError:
                _wait_released(path, cancelled, clock)
                return False
            resources.enter_context(again)
            try:
                latest, peer = inspect(again)
            except OSError:
                _wait_released(path, cancelled, clock)
                return False
            if peer[:2] != (pid, uid) or busy(latest) or broker_identity.matches(latest.get("identity"), local):
                return True
            peer_closed = _await_peer_close(again, cancelled, clock=clock)
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            if not peer_closed:
                LOG.warning(
                    "Legacy browser service (pid %s) did not close its status stream within 0.5 s; "
                    "holding status socket open until lock release",
                    pid,
                )
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(descriptor, signal.SIGINT)
                signalled = True
        LOG.info("Retiring idle browser service from a different build (pid %s)", pid)
        # Once SIGINT is sent, defer cancellation until the bounded lock-release wait
        # finishes so `again` stays open across the legacy broker's unwinding flush.
        _wait_released(path, None if signalled else cancelled, clock)
        if signalled and cancelled is not None and cancelled.is_set():
            raise CancelledError()
        return False


def retire_idle(path=None, *, clock=None):
    """BuildRefresh retires an old idle service without spawning a replacement."""
    from .browser_service import service_path

    target = Path(path or service_path())
    guard = handover(target) if clock is None else handover(target, clock=clock)
    with guard:
        return _retire_idle(target) if clock is None else _retire_idle(target, clock=clock)


def _retire_idle(target, *, clock=None):
    from .browser_service import BROKER_ABSENT, connect

    try:
        with connect(target, timeout=0.5) as connection:
            retained = reusable(connection, target) if clock is None else reusable(connection, target, clock=clock)
            if not retained:
                return False
        with connect(target, timeout=0.5) as connection:
            status, _peer = inspect(connection)
        return busy(status)
    except BROKER_ABSENT:
        return False
