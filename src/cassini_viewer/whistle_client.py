# SPDX-License-Identifier: GPL-3.0-or-later
"""One persistent recognition child; bounded replies and deterministic teardown."""

import contextlib
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

from .whistle_worker import PRIVACY, verify_bundle


class WhistleClient:
    def __init__(self, bundle, *, timeout=60):
        self.bundle = verify_bundle(bundle)
        self.timeout = timeout
        self.process = None
        self.selector = None
        self.ready = None
        self._buffer = b""

    def start(self):
        if self.process is not None:
            raise RuntimeError("Whistle worker is already started")
        self._buffer = b""
        self.ready = None
        env = dict(os.environ)
        env.update(PRIVACY)
        threads = int(env.get("CASSINI_WHISTLE_THREADS", "2"))
        if not 1 <= threads <= 32:
            raise ValueError("CASSINI_WHISTLE_THREADS must be between 1 and 32")
        env["CASSINI_WHISTLE_THREADS"] = str(threads)
        env["LD_PRELOAD"] = str(self.bundle / "libcassini_whistle_cpu.so")
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("whistle_worker.py")), str(self.bundle)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.ready = self._receive("ready")
        except BaseException:
            self.close()
            raise
        return self

    def _receive(self, expected):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self._buffer:
            remaining = max(0, deadline - time.monotonic())
            if not self.selector.select(remaining):
                self.close()
                raise RuntimeError("Whistle worker timed out")
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data or len(self._buffer) + len(data) > 1024 * 1024:
                raise RuntimeError("Whistle worker exited or exceeded the reply limit")
            self._buffer += data
        line, self._buffer = self._buffer.split(b"\n", 1)
        result = json.loads(line)
        if result.get("type") != expected:
            raise RuntimeError("Whistle worker failed: " + str(result.get("message", "invalid reply")))
        return result

    def transcribe(self, path):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Whistle worker is not running")
        try:
            self.process.stdin.write(
                (json.dumps(dict(type="transcribe", path=str(Path(path).resolve()))) + "\n").encode()
            )
            self.process.stdin.flush()
        except OSError as error:
            raise RuntimeError("Whistle worker stopped accepting requests") from error
        return self._receive("result")

    def close(self):
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(b'{"type":"quit"}\n')
                    self.process.stdin.flush()
                    self.process.wait(timeout=5)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=5)
            with contextlib.suppress(OSError):
                self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()
