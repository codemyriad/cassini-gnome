# SPDX-License-Identifier: GPL-3.0-or-later
"""Retire only an identified idle broker, off GTK and without touching captures."""

import fcntl
import logging
import os
import re
import signal
import socket
import struct
import threading
import time
from concurrent.futures import CancelledError
from pathlib import Path

from . import broker_identity

LOG = logging.getLogger(__name__)


def busy(status):
    if status.get("brokerBusy") is True:
        return True
    states, meetings = status.get("statuses"), status.get("meetings", [])
    if not isinstance(states, list) or not isinstance(meetings, list):
        return True  # No evidence that this process is idle.
    return any(
        not isinstance(row, dict)
        or row.get("recording")
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


def _released(path, cancelled):
    deadline = time.monotonic() + 5
    from .installed_build import _open_regular

    with _open_regular(path.with_suffix(".lock")) as lock:
        if os.fstat(lock.fileno()).st_uid != os.getuid():
            raise PermissionError("Browser service lock belongs to another user")
        while True:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("The older browser service has not finished closing") from None
                # A filesystem lock has no notification fd. Bound this worker
                # wait, and let a cancelled watcher interrupt it immediately.
                (cancelled or threading.Event()).wait(0.025)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
                return


def reusable(connection, path, *, cancelled=None):
    """True: retain this listener; False: it retired and released its lock."""
    from .browser_service import connect, request

    status, (pid, uid, _gid) = inspect(connection)
    local = broker_identity.target()
    if broker_identity.matches(status.get("identity"), local) or busy(status):
        return True
    if cancelled is not None and cancelled.is_set():
        raise CancelledError()
    if status.get("idleShutdown") is True:
        result = request("shutdown", path=path, identity=status.get("identity"))
        if result.get("shutdown") is not True:
            return True  # A capture started, or a different broker took over.
    else:
        # A pidfd pins the socket peer's process across /proc checks and the
        # final status read, so a recycled PID can never be signalled.
        try:
            descriptor = os.pidfd_open(pid)
        except (AttributeError, OSError):
            return True
        try:
            if not _legacy_process(pid, uid, path):
                return True
            with connect(path, timeout=0.5) as again:
                latest, peer = inspect(again)
            if peer[:2] != (pid, uid) or busy(latest) or broker_identity.matches(latest.get("identity"), local):
                return True
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            signal.pidfd_send_signal(descriptor, signal.SIGINT)
        finally:
            os.close(descriptor)
    LOG.info("Retiring idle browser service from a different build (pid %s)", pid)
    _released(path, cancelled)
    return False


def retire_idle(path=None):
    """BuildRefresh retires an old idle service without spawning a replacement."""
    from .browser_service import BROKER_ABSENT, connect, service_path

    target = Path(path or service_path())
    try:
        with connect(target, timeout=0.5) as connection:
            reusable(connection, target)
        with connect(target, timeout=0.5) as connection:
            status, _peer = inspect(connection)
        return busy(status)
    except BROKER_ABSENT:
        return False
