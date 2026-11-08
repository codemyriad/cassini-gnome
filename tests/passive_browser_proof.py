# SPDX-License-Identifier: GPL-3.0-or-later
"""Replay captured native-control packets through the broker, without capture.

The real outbound writer reaches a private socket peer which refuses every
Start as busy. No recording host is present. Worker admission is drained in
order, so an empty Start list is an observed decision, never a timed absence.
This helper also runs against the extracted, unmodified release package.
"""

import contextlib
import socket
import threading
from collections import deque
from unittest.mock import patch

from cassini_talk.browser_service import BrowserConnection, BrowserService, read_packet, send_packet


def replay(packets, parent, *, automatic=True, active=True, manual=False, before_dispatch=None):
    service = BrowserService(parent / "settings.json", require_application=True, command_timeout=2)
    tasks, starts, errors = deque(), [], []
    barrier = threading.Event()
    broker, extension = socket.socketpair()
    broker_stream, extension_stream = broker.makefile("rwb"), extension.makefile("rwb")
    browser = BrowserConnection(broker_stream, "Chromium", "private-d80-browser", connection=broker)
    service.browsers[browser.id] = browser
    app_server, app_client = socket.socketpair()
    app_stream = app_client.makefile("rwb")
    application = threading.Thread(target=service.handle, args=(app_server,))
    application.start()
    send_packet(app_stream, dict(role="application", active=active, generation=1))
    assert read_packet(app_stream)["ready"] is True
    service.auto_record = automatic

    def forbidden(*_args, **_kwargs):
        raise AssertionError("The passive proof must never create a capture or fallback")

    def receiver():
        try:
            while (packet := read_packet(extension_stream)) is not None:
                if packet.get("type") == "proofBarrier":
                    barrier.set()
                elif packet.get("action") == "start":
                    pending = browser.pending[packet["requestId"]]
                    starts.append(dict(packet, automatic=pending["automatic"]))
                    # This is a test refusal at the actual wire boundary. It
                    # avoids both extension capture and native fallback.
                    service.update(
                        browser,
                        dict(
                            type="result",
                            requestId=packet["requestId"],
                            ok=False,
                            code="busy",
                            error="D80 proof: capture forbidden",
                        ),
                    )
        except Exception as error:
            if not service.closed.is_set():
                errors.append(error)

    reader = threading.Thread(target=receiver)
    reader.start()
    try:
        with (
            patch.object(service.workers, "submit", lambda function, *args: tasks.append((function, args))),
            patch.object(service, "start_fallback", forbidden),
            patch("cassini_talk.browser_capture.BrowserCapture.__init__", forbidden),
            patch("cassini_talk.native_recorder.NativeRecorder.__init__", forbidden),
        ):
            for packet in packets:
                send_packet(extension_stream, packet)
                service.update(browser, read_packet(broker_stream))
            if before_dispatch is not None:
                before_dispatch(service, browser)
            while tasks:
                function, args = tasks.popleft()
                function(*args)
            if manual:
                tab = next(iter(browser.meetings))
                try:
                    service.command(f"{browser.id}:{tab}", "start", user=True)
                except ValueError as error:
                    assert str(error) == "D80 proof: capture forbidden"
            browser.send(dict(type="proofBarrier"))
            assert barrier.wait(2), "Real broker writer must finish before assertions"
            assert not errors, errors
            assert not service.captures
            assert not list(parent.glob("**/session.json"))
            return starts
    finally:
        service.closed.set()
        browser.shutdown()
        for connection in (extension, broker, app_client):
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        reader.join(2)
        application.join(2)
        if browser.writer:
            browser.writer.join(2)
            assert not browser.writer.is_alive()
        assert not reader.is_alive() and not application.is_alive()
        for stream in (broker_stream, extension_stream, app_stream):
            stream.close()
        for connection in (extension, broker, app_client):
            connection.close()
        service.workers.shutdown(wait=True)
