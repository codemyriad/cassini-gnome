# SPDX-License-Identifier: GPL-3.0-or-later
"""Event-driven teardown of a test-owned UNIX broker, without a poll-period wait."""

import errno
import socket

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
