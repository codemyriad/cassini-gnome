# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit private broker requests and event-driven teardown of owned fixtures."""

import contextlib
import errno
import io
import socket
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from durable import DURABLE_BOUND, tracks_bound


def stop_broker(service, worker, path):
    with service.lock:
        tracks = [len(c.host.session.manifest["tracks"]) for c in service.captures.values() if c.directory is not None]
    service.closed.set()
    # Wake accept() now. An idle broker otherwise notices closed only at its
    # next 500 ms poll, accumulating tens of seconds across the source suite.
    # The local connection closes immediately; no control command is sent.
    try:
        with socket.socket(socket.AF_UNIX) as wake:
            wake.settimeout(1)
            wake.connect(str(path))
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise
    # On its way out the broker saves every capture still open, one after the
    # other and each durably: its exit is bounded against those captures' work.
    bound = max(DURABLE_BOUND, sum(tracks_bound(count) for count in tracks))
    worker.join(bound)
    assert not worker.is_alive(), (
        f"Private browser broker did not exit within {bound} s ({len(tracks)} captures) "
        "after shutdown and socket wakeup"
    )


def authorize_native_start(service, *, call_generation=None, microphone=True):
    """Issue an actual verified manual host ask for a synthetic native writer."""
    from cassini_talk.browser_service import BrowserConnection
    from cassini_talk.call_admission import CAPABILITIES

    peer = BrowserConnection(io.BytesIO())

    def acknowledge(packet):
        if packet.get("type") == "command":
            service.update(
                peer, dict(type="result", requestId=packet["requestId"], ok=True, status=dict(recording=False))
            )

    peer.send = acknowledge
    with service.lock:
        service.browsers[peer.id] = peer
    try:
        service.update(
            peer, dict(type="hello", version=1, capabilities=sorted(CAPABILITIES | {"independent-sessions-v1"}))
        )
        service.update(peer, dict(type="configured", capabilities=sorted(CAPABILITIES)))
        row = dict(
            tabId=2,
            title="Synthetic manual writer",
            url="https://fixture.invalid/manual",
            sourceCount=1,
            pageState="present",
            callGeneration=call_generation,
        )
        service.update(peer, dict(type="meetings", meetings=[row]))
        service.update(peer, dict(type="status", recording=False))
        service.command(f"{peer.id}:2", "start", user=True, microphone=microphone)
        return next(reversed(peer.commands))
    finally:
        with service.lock:
            service.browsers.pop(peer.id, None)


@contextlib.contextmanager
def remote_native_start(path, *, microphone):
    """Obtain an actual host ask over private sockets, keeping its peer alive."""
    from cassini_talk.browser_service import connect, read_packet, request, send_packet

    url = "https://fixture.invalid/native/" + str(uuid4())
    hello = dict(type="hello", version=1, capabilities=["independent-sessions-v1", "call-signals-v1"])
    with connect(path, timeout=DURABLE_BOUND) as connection, connection.makefile("rwb") as stream:
        send_packet(stream, dict(role="extension", browserName="Synthetic native fixture"))
        send_packet(stream, hello)
        assert read_packet(stream)["type"] == "configure"
        send_packet(
            stream,
            dict(
                type="meetings",
                meetings=[dict(tabId=2, title="Synthetic manual writer", url=url, sourceCount=1, pageState="present")],
            ),
        )
        send_packet(stream, dict(type="status", recording=False))
        # Replies are ordered after the preceding inventory on this control pipe.
        send_packet(stream, hello)
        assert read_packet(stream)["type"] == "configure"
        send_packet(stream, dict(type="configured", capabilities=["call-signals-v1"]))
        row = next(row for row in request(path=path)["meetings"] if row["url"] == url)
        with ThreadPoolExecutor(1) as pool:
            asking = pool.submit(
                request, "start-all", path=path, microphone=microphone, only=dict(id=row["id"], callGeneration=None)
            )
            command = read_packet(stream)
            assert command["type"] == "command" and command["action"] == "start"
            send_packet(
                stream, dict(type="result", requestId=command["requestId"], ok=True, status=dict(recording=False))
            )
            result = asking.result(DURABLE_BOUND)
            assert result["attempted"] == 1 and not result["failures"]
        yield command["requestId"]
