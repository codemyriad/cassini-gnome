# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep broker stderr beyond the GUI's lifetime, including startup failures."""

import ctypes
import fcntl
import logging
import logging.handlers
import os
import signal
import stat
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 2
IDENTIFIER = b"cassini-browser-service"


def journal_stream():
    """A journal stream opened before importing the child's application code."""
    try:
        library = ctypes.CDLL("libsystemd.so.0")
        function = library.sd_journal_stream_fd
        function.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        function.restype = ctypes.c_int
        descriptor = function(IDENTIFIER, 6, 1)
        return descriptor if descriptor >= 0 else None
    except (OSError, AttributeError):
        return None


def _private_file(path, access):
    descriptor = os.open(path, access | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise PermissionError("Browser diagnostic log must be a user-owned regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_log(path):
    return os.fdopen(_private_file(path, os.O_WRONLY | os.O_APPEND), "a", encoding="utf-8", errors="backslashreplace")


class PrivateLog(logging.handlers.RotatingFileHandler):
    """Competing launchers serialize rotation and reopen any replaced inode."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self._rotation_fd = _private_file(self.baseFilename + ".lock", os.O_RDWR)
        except BaseException:
            super().close()
            raise

    def _open(self):
        return _open_log(self.baseFilename)

    def _refresh(self):
        try:
            current = os.stat(self.baseFilename, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        opened = os.fstat(self.stream.fileno()) if self.stream is not None else None
        if current is None or opened is None or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.stream = self._open()

    def shouldRollover(self, record):
        if self.maxBytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()
        size = os.fstat(self.stream.fileno()).st_size
        message = (self.format(record) + self.terminator).encode("utf-8", "backslashreplace")
        return size > 0 and size + len(message) >= self.maxBytes

    def emit(self, record):
        with self.lock:
            fcntl.flock(self._rotation_fd, fcntl.LOCK_EX)
            try:
                self._refresh()
                super().emit(record)
            finally:
                fcntl.flock(self._rotation_fd, fcntl.LOCK_UN)

    def close(self):
        with self.lock:
            try:
                super().close()
            finally:
                if getattr(self, "_rotation_fd", None) is not None:
                    os.close(self._rotation_fd)
                    self._rotation_fd = None


def launch(command, *, env):
    descriptor = journal_stream()
    if descriptor is not None:
        try:
            return subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=descriptor,
                start_new_session=True,
            )
        finally:
            os.close(descriptor)
    root = Path(env.get("XDG_STATE_HOME", Path(env.get("HOME", str(Path.home()))) / ".local/state")) / "cassini"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "browser-service.log"
    # The independent helper drains raw stderr and rotates it. A reader in the
    # GUI would die when its window/process exits, breaking the broker's pipe.
    with _open_log(path) as bootstrap:
        return subprocess.Popen(
            [sys.executable, "-m", "cassini_talk.broker_logging", str(path), *command],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=bootstrap,
            start_new_session=True,
        )


def collect(path, command):
    handler = PrivateLog(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s pid=%(process)d %(message)s"))
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as process:

            def forward(number, _frame):
                # The child may already have exited while stderr was draining.
                with suppress(ProcessLookupError):
                    process.send_signal(number)

            signal.signal(signal.SIGTERM, forward)
            signal.signal(signal.SIGINT, forward)
            while text := process.stderr.readline(8192):
                record = logging.LogRecord("cassini.browser", logging.INFO, "", 0, text.rstrip("\n"), (), None)
                record.process = process.pid
                handler.emit(record)
            return process.wait()
    finally:
        handler.close()


if __name__ == "__main__":
    raise SystemExit(collect(Path(sys.argv[1]), sys.argv[2:]))
