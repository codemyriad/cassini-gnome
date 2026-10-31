# SPDX-License-Identifier: GPL-3.0-or-later
"""Event-driven teardown of a test-owned UNIX broker, without a poll-period wait."""

import errno
import socket


def stop_broker(service, worker, path):
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
    worker.join(2)
    assert not worker.is_alive(), "Private browser broker did not exit after shutdown and socket wakeup"
