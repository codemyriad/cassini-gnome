# SPDX-License-Identifier: GPL-3.0-or-later
"""Native framing stays in Chrome's child; capture ownership stays in the broker."""

from . import browser_service
from .recording_session import ProtocolError


class BrowserAudioProxy:
    def __init__(self, *, path=None):
        target = path or browser_service.ensure_service()
        self.connection = browser_service.connect(target)
        self.stream = self.connection.makefile("rwb")
        self.closed = False
        try:
            browser_service.send_packet(self.stream, dict(role="audio"))
            ready = browser_service.read_packet(self.stream)
            if ready != dict(ready=True, protocol=2):
                raise OSError("Independent browser capture is unavailable")
        except Exception:
            self.close()
            raise

    def handle(self, message):
        browser_service.send_packet(self.stream, message)
        result = browser_service.read_packet(self.stream)
        if result is None:
            raise OSError("The browser capture broker disconnected")
        if result.get("type") == "error":
            raise ProtocolError(result.get("code", "state"), result.get("message", "Browser capture failed"))
        return result

    def close(self, *_args, **_kwargs):
        if not self.closed:
            self.closed = True
            self.stream.close()
            self.connection.close()
