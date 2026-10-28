# SPDX-License-Identifier: GPL-3.0-or-later
"""Application-scoped inventory and lifetime lease, independent of any window."""

import contextlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from gi.repository import GLib

from . import browser_service

LOG = logging.getLogger(__name__)


class BrowserWatch:
    def __init__(self, changed, *, path=None):
        self.changed, self.path = changed, path
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.lease = None
        self.active = True
        self.generation = time.monotonic_ns()
        self.future = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-browser-watch")
        self.timer = GLib.timeout_add(500, self.poll)
        self.poll()

    def _read(self):
        target = self.path or browser_service.ensure_service(cancelled=self.closed)
        with self.lock:
            missing = self.lease is None
        if missing:
            lease = browser_service.connect(target, timeout=0.8)
            try:
                with lease.makefile("rwb") as stream:
                    with self.lock:
                        generation, active = self.generation, self.active
                    browser_service.send_packet(stream, dict(role="application", active=active, generation=generation))
                    if browser_service.read_packet(stream) != dict(ready=True, protocol=2):
                        raise OSError("Application browser connection unavailable")
                lease.setblocking(False)
                with self.lock:
                    if self.closed.is_set():
                        lease.close()
                    else:
                        self.lease = lease
                        if generation != self.generation:
                            lease.sendall(
                                json.dumps(dict(active=self.active, generation=self.generation)).encode() + b"\n"
                            )
            except Exception:
                lease.close()
                raise
        if self.closed.is_set():
            return None
        with browser_service.connect(target, timeout=0.25) as connection, connection.makefile("rwb") as stream:
            browser_service.send_packet(stream, dict(role="app", action="status"))
            value = browser_service.read_packet(stream)
            if value is None or value.get("error"):
                raise OSError("Browser inventory unavailable")
            return value

    def poll(self):
        if self.closed.is_set():
            return GLib.SOURCE_REMOVE
        if self.future is None:
            self.future = self.executor.submit(self._read)
            self.future.add_done_callback(lambda future: GLib.idle_add(self._received, future))
        return GLib.SOURCE_CONTINUE

    def _received(self, future):
        self.future = None
        if not self.closed.is_set():
            try:
                value = future.result()
            except Exception:
                LOG.exception("Browser inventory update failed")
                with self.lock:
                    if self.lease is not None:
                        self.lease.close()
                        self.lease = None
            else:
                if value is not None:
                    self.changed(value)
        return GLib.SOURCE_REMOVE

    def set_active(self, active):
        with self.lock:
            self.active = active
            self.generation = max(self.generation + 1, time.monotonic_ns())
            if self.lease is not None:
                try:
                    self.lease.sendall(json.dumps(dict(active=active, generation=self.generation)).encode() + b"\n")
                except OSError:
                    self.lease.close()
                    self.lease = None

    def pause(self):
        self.set_active(False)

    def resume(self):
        self.set_active(True)

    def close(self):
        self.closed.set()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        with self.lock:
            if self.lease is not None:
                with contextlib.suppress(OSError):
                    self.lease.shutdown(2)
                self.lease.close()
                self.lease = None
        self.executor.shutdown(wait=False, cancel_futures=True)
