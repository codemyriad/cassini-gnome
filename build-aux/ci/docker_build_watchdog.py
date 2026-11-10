#!/usr/bin/env python3
# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stream a build's bytes, bounding silence separately from its total lifetime."""

import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Progress:
    started: float
    stall: int
    overall: int
    last_output: float | None = None

    def __post_init__(self):
        self.last_output = self.started

    def observe(self, now, data):
        if data:
            self.last_output = now

    def remaining(self, now):
        return max(0, min(self.started + self.overall, self.last_output + self.stall) - now)

    def expired(self, now):
        if now >= self.started + self.overall:
            return "overall"
        if now >= self.last_output + self.stall:
            return "silence"
        return None


class ProcessEvents:
    """Output and process completion wake the same selector; no polling sleeps."""

    def __init__(self, process):
        self.process = process
        self.selector = selectors.DefaultSelector()
        self.reader, self.writer = socket.socketpair()
        os.set_blocking(process.stdout.fileno(), False)
        self.selector.register(process.stdout, selectors.EVENT_READ, "output")
        self.selector.register(self.reader, selectors.EVENT_READ, "exit")
        self.waiter = threading.Thread(target=self._exited, name="cassini-build-exit", daemon=True)
        self.waiter.start()

    def _exited(self):
        self.process.wait()
        try:
            self.writer.send(b"x")
        except OSError:
            pass
        finally:
            self.writer.close()

    def poll(self, timeout):
        events = []
        for key, _ in self.selector.select(timeout):
            if key.data == "exit":
                self.reader.recv(1)
                self.selector.unregister(self.reader)
                events.append(("exit", self.process.returncode))
            else:
                data = os.read(key.fd, 65536)
                if data:
                    events.append(("output", data))
                else:
                    self.selector.unregister(key.fileobj)
                    events.append(("eof", None))
        return events

    def close(self):
        self.selector.close()
        self.reader.close()
        self.process.stdout.close()
        self.waiter.join()


def supervise(events, progress, clock, emit):
    exited, eof, status = False, False, None
    while not (exited and eof):
        now = clock()
        reason = progress.expired(now)
        if reason:
            return None, reason
        for kind, value in events.poll(progress.remaining(now)):
            if kind == "output":
                progress.observe(clock(), value)
                emit(value)
            elif kind == "exit":
                exited, status = True, value
            elif kind == "eof":
                eof = True
    return status, None


def stop(process, *, grace=10, send=os.killpg, pause=None, clock=time.monotonic):
    """Signal only the process group created for this exact build."""
    pause = pause or threading.Event().wait
    began = clock()
    try:
        send(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return False
    escalated = False
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        escalated = True
    else:
        # A parent may exit while a child ignores TERM and holds its log pipe.
        try:
            send(process.pid, 0)
        except ProcessLookupError:
            return False
        pause(max(0, grace - (clock() - began)))
    with suppress(ProcessLookupError):
        send(process.pid, signal.SIGKILL)
    process.wait()
    return escalated


class Interrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def interrupted(signum, _frame):
    raise Interrupted(signum)


def seconds(name, default, maximum):
    raw = os.environ.get(name) or str(default)
    if not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= maximum:
        raise ValueError(f"{name} must be whole seconds from 1 to {maximum}")
    return int(raw)


def emit(data):
    sys.stderr.buffer.write(data)
    sys.stderr.buffer.flush()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 4 or args[1:3] != ["docker", "build"]:
        print("Usage: docker-build-bounded.sh NAME docker build ARG...", file=sys.stderr)
        return 2
    name = args[0]
    try:
        stall = seconds("CASSINI_HARNESS_BUILD_STALL_TIMEOUT", 180, 300)
        overall = seconds("CASSINI_HARNESS_BUILD_TIMEOUT", 900, 900)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    arguments = [arg for arg in args[3:] if arg not in ("-q", "--quiet")]
    print(
        f"Harness build [{name}]: {stall}s without output, {overall}s overall (+10s kill grace)",
        file=sys.stderr,
        flush=True,
    )
    previous = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGTERM, signal.SIGINT)}
    process, events = None, None
    try:
        with tempfile.TemporaryDirectory(prefix="cassini-build-", dir="/tmp") as workspace:
            receipt = Path(workspace) / "image"
            started = time.monotonic()
            process = subprocess.Popen(
                ["docker", "build", "--progress=plain", "--iidfile", str(receipt), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=True,
            )
            events = ProcessEvents(process)
            status, reason = supervise(events, Progress(started, stall, overall), time.monotonic, emit)
            if reason:
                status = 137 if stop(process) else 124
                message = (
                    f"made no output progress for {stall}s"
                    if reason == "silence"
                    else f"exceeded its {overall}s overall limit"
                )
                print(f"Harness build [{name}] {message} (exit {status}); tests were not run.", file=sys.stderr)
                return status
            if status:
                status = status if status > 0 else 128 - status
                print(f"Harness build [{name}] failed (exit {status}); tests were not run.", file=sys.stderr)
                return status
            image = receipt.read_text().strip() if receipt.exists() else ""
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                print(f"Harness build [{name}] returned no valid image ID", file=sys.stderr)
                return 1
            print(image)
            return 0
    except Interrupted as error:
        # Ignore repeated cancellation while terminating this owned group.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        if process is not None:
            stop(process)
        print(f"Harness build [{name}] cancelled by signal {error.signum}; tests were not run.", file=sys.stderr)
        return 128 + error.signum
    except BaseException:
        if process is not None:
            stop(process)
        raise
    finally:
        if events is not None:
            events.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
