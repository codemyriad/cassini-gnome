# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent Native Messaging transport for desktop meeting control."""

import contextlib
import logging
import os
import sys
import threading

from .browser_service import connect, ensure_service, read_packet, send_packet
from .recording_session import read_message, write_message

LOG = logging.getLogger(__name__)


def main(*, input_stream=None, output_stream=None, path=None):
    # An unbuffered descriptor lets a daemon reader block without holding the
    # interpreter's buffered stdin lock when the broker disconnects first.
    incoming = open(sys.stdin.fileno(), "rb", buffering=0, closefd=False) if input_stream is None else input_stream  # noqa: SIM115
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        override = os.environ.get("CASSINI_CONTROL_SOCKET")
        target = path or override or ensure_service()
        with connect(target) as connection, connection.makefile("rwb") as stream:
            if not override:
                send_packet(stream, {"role": "extension"})
            done = threading.Event()
            failed = []

            def commands():
                try:
                    while not done.is_set() and (packet := read_packet(stream)) is not None:
                        write_message(outgoing, packet)
                except (OSError, ValueError) as error:
                    if not done.is_set():
                        failed.append(error)
                finally:
                    done.set()

            def events():
                try:
                    while not done.is_set() and (packet := read_message(incoming)) is not None:
                        send_packet(stream, packet)
                except (OSError, ValueError) as error:
                    if not done.is_set():
                        failed.append(error)
                finally:
                    done.set()

            threads = [threading.Thread(target=pump, daemon=True) for pump in (commands, events)]
            for thread in threads:
                thread.start()
            done.wait()
            with contextlib.suppress(OSError):
                connection.shutdown(2)
            for thread in threads:
                thread.join(timeout=1)
            if failed:
                raise failed[0]
        return 0
    except (OSError, ValueError) as error:
        LOG.warning("Browser control connection failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
