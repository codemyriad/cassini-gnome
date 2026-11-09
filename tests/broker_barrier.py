# SPDX-License-Identifier: GPL-3.0-or-later
"""Private control/worker/writer barriers for broker negative assertions."""

import threading
from uuid import uuid4

from durable import DURABLE_BOUND

from cassini_talk.browser_service import read_packet, send_packet


class TrackedWorkers:
    def __init__(self, pool):
        self.pool = pool
        self.changed = threading.Condition()
        self.pending = set()

    def __getattr__(self, name):
        return getattr(self.pool, name)

    def submit(self, function, *args, **kwargs):
        marker = object()
        with self.changed:
            self.pending.add(marker)
        try:
            future = self.pool.submit(function, *args, **kwargs)
        except BaseException:
            self._done(marker)
            raise
        future.add_done_callback(lambda _future: self._done(marker))
        return future

    def _done(self, marker):
        with self.changed:
            self.pending.remove(marker)
            self.changed.notify_all()

    def idle(self):
        with self.changed:
            assert self.changed.wait_for(lambda: not self.pending, timeout=DURABLE_BOUND), (
                "Broker workers did not finish"
            )


class BrokerBarrier:
    def __init__(self, service):
        self.service = service
        self.requests = {}
        self.lock = threading.Lock()
        self.workers = service.workers = TrackedWorkers(service.workers)
        update = service.update

        def observed(browser, packet):
            if packet.get("type") != "_testInputBarrier":
                return update(browser, packet)
            with self.lock:
                entry = self.requests.get(packet.get("token"))
                if entry is not None:
                    entry[1].append(browser)
                    entry[0].set()

        service.update = observed
        service.test_barrier = self

    def no_command(self, connection, stream):
        """Call after the test has awaited its manual RPC futures.

        Fence earlier input on this reader, then the broker-owned worker bodies
        (including nested submissions), then its FIFO writer. Clock-driven
        scenarios must advance their clock explicitly before probing.
        """
        previous_timeout = connection.gettimeout()
        token = uuid4().hex
        event, browsers = threading.Event(), []
        with self.lock:
            self.requests[token] = event, browsers
        try:
            connection.settimeout(2)
            # A fixture-only marker: earlier real input and its policy calls
            # have returned on this reader before the marker is intercepted.
            # No production event is fabricated and no liveness clock changes.
            send_packet(stream, dict(type="_testInputBarrier", token=token))
            assert event.wait(DURABLE_BOUND), "Broker did not finish processing control input"
            self.workers.idle()
            barrier = dict(type="_testWriterBarrier", action="_testWriterBarrier", token=token)
            browser = browsers[0]
            if browser.closed:
                return read_packet(stream) is None
            browser.send(barrier)
            packet = read_packet(stream)
            return packet is None or packet == barrier
        finally:
            with self.lock:
                self.requests.pop(token, None)
            connection.settimeout(previous_timeout)


def no_command(service, connection, stream):
    return service.test_barrier.no_command(connection, stream)
