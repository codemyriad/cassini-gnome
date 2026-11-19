# SPDX-License-Identifier: GPL-3.0-or-later
"""One persistent recognition child; bounded replies and deterministic teardown."""

import contextlib
import json
import logging
import os
import selectors
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError
from pathlib import Path

from .speech_threads import recognition_threads
from .whistle_worker import PRIVACY, verify_bundle

LOG = logging.getLogger(__name__)


class WhistleClient:
    worker_name = "Whistle"

    def __init__(self, bundle, *, timeout=60):
        self.bundle = verify_bundle(bundle)
        self.timeout = timeout
        self.process = None
        self.selector = None
        self.ready = None
        self._buffer = b""
        self.cancelled = threading.Event()
        self._process_lock = threading.Lock()

    def worker_command(self):
        return [sys.executable, str(Path(__file__).with_name("whistle_worker.py")), str(self.bundle)]

    def worker_environment(self):
        env = dict(os.environ)
        env.update(PRIVACY)
        override = env.get("CASSINI_WHISTLE_THREADS")
        threads = recognition_threads() if override is None else int(override)
        if not 1 <= threads <= 32:
            raise ValueError("CASSINI_WHISTLE_THREADS must be between 1 and 32")
        env["CASSINI_WHISTLE_THREADS"] = str(threads)
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["OMP_NUM_THREADS"] = "1"
        env["LD_PRELOAD"] = str(self.bundle / "libcassini_whistle_cpu.so")
        return env

    def start(self):
        if self.cancelled.is_set():
            raise CancelledError()
        if self.process is not None:
            raise RuntimeError(self.worker_name + " worker is already started")
        self._buffer = b""
        self.ready = None
        env = self.worker_environment()
        if self.cancelled.is_set():
            raise CancelledError()
        # Never hold _process_lock across Popen(): main-thread cancel() must return
        # immediately without waiting for child spawn, and start() reaps if cancelled.
        process = subprocess.Popen(
            self.worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        with self._process_lock:
            cancelled = self.cancelled.is_set()
            if not cancelled:
                self.process = process
        if cancelled:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=5)
            with contextlib.suppress(OSError):
                process.stdin.close()
            with contextlib.suppress(OSError):
                process.stdout.close()
            raise CancelledError()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.ready = self._receive("ready")
        except BaseException:
            self.close()
            raise
        return self

    def is_alive(self):
        with self._process_lock:
            return self.process is not None and self.process.poll() is None and not self.cancelled.is_set()

    def _receive_line(self, timeout, *, raise_on_timeout=True):
        deadline = time.monotonic() + max(0.0, timeout)
        while b"\n" not in self._buffer:
            remaining = max(0.0, deadline - time.monotonic())
            if self.cancelled.is_set():
                raise CancelledError()
            if self.selector is None or self.process is None:
                raise RuntimeError(self.worker_name + " worker is not running")
            if not self.selector.select(min(remaining, 0.2)):
                if time.monotonic() < deadline:
                    continue
                if not raise_on_timeout:
                    return None
                self.cancel()
                self.close()
                raise RuntimeError(self.worker_name + " worker timed out")
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data or len(self._buffer) + len(data) > 1024 * 1024:
                raise RuntimeError(self.worker_name + " worker exited or exceeded the reply limit")
            self._buffer += data
        line, self._buffer = self._buffer.split(b"\n", 1)
        if self.cancelled.is_set():
            raise CancelledError()
        return json.loads(line)

    def _receive(self, expected=None):
        result = self._receive_line(self.timeout, raise_on_timeout=True)
        if expected is not None and result.get("type") != expected:
            raise RuntimeError(self.worker_name + " worker failed: " + str(result.get("message", "invalid reply")))
        return result

    def start_follow(self, directory, model_identity, *, poll_interval_ms=50):
        if not self.is_alive():
            raise RuntimeError(self.worker_name + " worker is not running")
        packet = (
            json.dumps(
                dict(
                    type="follow",
                    directory=str(Path(directory).resolve()),
                    model=model_identity,
                    pollIntervalMs=poll_interval_ms,
                )
            ).encode()
            + b"\n"
        )
        if len(packet) > 512 * 1024:
            raise ValueError("Worker request exceeds limit")
        try:
            self.process.stdin.write(packet)
            self.process.stdin.flush()
        except OSError as error:
            raise RuntimeError(self.worker_name + " worker stopped accepting requests") from error

    def poll_follow_event(self, timeout=0.05):
        return self._receive_line(timeout, raise_on_timeout=False)

    def stop_follow(self, *, drain=True, timeout=3.0, on_event=None):
        """Drain or stop follow mode within a bounded deadline; return summary if warm, else None."""
        if not self.is_alive():
            self.close()
            return None
        try:
            packet = (json.dumps(dict(type="follow-stop", drain=bool(drain))) + "\n").encode()
            self.process.stdin.write(packet)
            self.process.stdin.flush()
        except OSError:
            self.cancel()
            self.close()
            return None
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.cancel()
                self.close()
                return None
            try:
                value = self._receive_line(remaining, raise_on_timeout=False)
            except (CancelledError, OSError, RuntimeError, ValueError):
                self.cancel()
                self.close()
                return None
            if value is None:
                self.cancel()
                self.close()
                return None
            msg_type = value.get("type")
            if msg_type == "follow-stopped":
                return value
            if msg_type in ("follow-cache", "follow-idle"):
                if on_event is not None:
                    on_event(value)
                continue
            self.cancel()
            self.close()
            return None

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

    def process_tracks(self, tracks, progress):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Whistle worker is not running")
        message = dict(type="pipeline", tracks=tracks)
        if getattr(self, "keep_for_voices", False):
            message["keepForVoices"] = True
        if getattr(self, "diarization", None) is not None:
            message["diarization"] = self.diarization
        if getattr(self, "recognition_cache", None) is not None:
            message["recognitionCache"] = self.recognition_cache
        packet = json.dumps(message).encode() + b"\n"
        if len(packet) > 512 * 1024:
            raise ValueError("Worker request exceeds limit")
        try:
            self.process.stdin.write(packet)
            self.process.stdin.flush()
        except OSError as error:
            raise RuntimeError("Whistle worker stopped accepting requests") from error
        while True:
            value = self._receive()
            if value.get("type") == "complete":
                return value
            if value.get("type") not in ("planning", "planned", "progress", "recognised", "diarizing", "diarized"):
                raise RuntimeError("Whistle worker failed: " + str(value.get("message", "invalid reply")))
            progress(value)

    def voiceprints(self, tracks, options, heartbeat=lambda: None):
        packet = json.dumps(dict(type="voiceprints", tracks=tracks, options=options)).encode() + b"\n"
        if len(packet) > 512 * 1024:
            raise ValueError("Voice request exceeds limit")
        self.process.stdin.write(packet)
        self.process.stdin.flush()
        result = {}
        while True:
            value = self._receive()
            heartbeat()
            if value.get("type") == "voices-complete":
                return result
            if value.get("type") == "voice-print":
                speaker = value["speaker"]
                if speaker in result or len(result) >= 512:
                    raise ValueError("Duplicate or excess voice print")
                result[speaker] = {k: value[k] for k in ("embedding", "seconds", "windows")}
            elif value.get("type") == "voice-progress":
                if "diagnostic" in value:  # The child's stderr is not kept.
                    LOG.warning("Voice diagnostic: %s", value["diagnostic"])
            else:
                raise RuntimeError("Voice worker failed: " + str(value.get("message", "invalid reply")))

    def cancel(self):
        """Main-thread safe: signal only; the owning IO thread reaps/closes."""
        self.cancelled.set()
        with self._process_lock:
            process = self.process
            if process is not None and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()

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
            with self._process_lock:
                self.process = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()
