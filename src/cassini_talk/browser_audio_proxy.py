# SPDX-License-Identifier: GPL-3.0-or-later
"""Native framing stays in Chrome's child; capture ownership stays in the broker."""

import contextlib
import select
import socket
import threading

from . import browser_service
from .recording_session import ProtocolError


class BrowserAudioProxy:
    def __init__(self, *, path=None, spawn=True):
        # A resume can only target a capture inside a running broker, so it
        # never starts one: no broker means no target, and no folder.
        target = path or (browser_service.ensure_service() if spawn else browser_service.service_path())
        self.connection = browser_service.connect(target)
        self.stream = self.connection.makefile("rwb")
        self.closed = False
        try:
            browser_service.send_packet(self.stream, dict(role="audio"))
            ready = browser_service.read_packet(self.stream)
            if not isinstance(ready, dict) or ready.get("ready") is not True or ready.get("protocol") != 2:
                raise OSError("Independent browser capture is unavailable")
            from .broker_identity import matches

            self.identity = ready.get("identity")
            self.legacy = self.identity is None
            self.broker_stale = not matches(self.identity)
        except Exception:
            self.close()
            raise

    def handle(self, message):
        browser_service.send_packet(self.stream, message)
        result = browser_service.read_packet(self.stream)
        if result is None:
            raise OSError("The browser capture broker disconnected")
        if result.get("type") == "error":
            if result.get("scope") in ("source", "command"):
                return result
            raise ProtocolError(result.get("code", "state"), result.get("message", "Browser capture failed"))
        return result

    def watch(self, closed):
        """Call closed() once the broker shuts this transport down.

        The broker only ever answers, so it is otherwise noticed only when the
        browser next sends something. A transport the broker closed for stale
        PCM sends nothing more, and the extension would keep its session (and
        the tab) until then. POLLRDHUP fires on the peer's shutdown even while
        a reply is still buffered for handle().
        """
        poller = select.poll()
        poller.register(self.connection.fileno(), select.POLLRDHUP)

        def wait():
            with contextlib.suppress(OSError):
                poller.poll()
            if not self.closed:
                closed()

        threading.Thread(target=wait, daemon=True, name="cassini-audio-proxy-watch").start()

    def close(self, *_args, **_kwargs):
        if not self.closed:
            self.closed = True
            # Wake the watcher (POLLHUP on this end) before the descriptor goes.
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            self.stream.close()
            self.connection.close()
