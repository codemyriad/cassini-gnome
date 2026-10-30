# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent Native Messaging transport for desktop meeting control."""

import contextlib
import logging
import os
import re
import sys
import threading
from pathlib import Path

from .browser_service import connect, ensure_service, read_packet, send_packet
from .recording_session import read_message, write_message

LOG = logging.getLogger(__name__)


def parent_browser_info():
    """Optional local broker hint; never change the shared extension protocol."""
    # The host-side relay sees Chromium's real PID; the nested sandbox cannot.
    # Accept this local hint only in Flatpak, never from extension messages.
    if os.environ.get("FLATPAK_ID") in {"com.gocassini.Viewer", "com.gocassini.Viewer.Devel"}:
        name = os.environ.get("CASSINI_HOST_BROWSER_NAME")
        instance = os.environ.get("CASSINI_HOST_BROWSER_INSTANCE", "")
        if name in {"Chrome", "Chromium", "Edge", "Brave"} and re.fullmatch(r"[0-9]{1,20}:[0-9]{1,30}", instance):
            return dict(browserName=name, browserInstance=instance)
    names = {
        "chrome": "Chrome",
        "google-chrome": "Chrome",
        "google-chrome-stable": "Chrome",
        "chromium": "Chromium",
        "chromium-browser": "Chromium",
        "brave": "Brave",
        "brave-browser": "Brave",
        "msedge": "Edge",
        "microsoft-edge": "Edge",
        "firefox": "Firefox",
    }
    pid = os.getppid()
    for _ in range(5):
        if pid <= 1:
            break
        try:
            executable = Path(f"/proc/{pid}/exe").readlink().name.lower()
            if executable in names:
                # A worker/control-host restart keeps the same browser process
                # and global tab IDs. Include its kernel start tick to avoid
                # confusing a later process that reuses the PID. This stays on
                # the local same-user broker transport, outside the protocol.
                start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                return dict(browserName=names[executable], browserInstance=f"{pid}:{start}")
            status = Path(f"/proc/{pid}/status").read_text()
            pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, ValueError, StopIteration):
            break
    return dict(browserName=None, browserInstance=None)


def parent_browser_name():
    return parent_browser_info()["browserName"]


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
            # The link stays open for the browser's lifetime and the broker
            # writes only when it has a command, so an idle read must not time
            # out: that dropped the link every 30 s and reset the broker's
            # per-browser state. EOF from either side still ends it.
            connection.settimeout(None)
            if not override:
                send_packet(stream, {"role": "extension", **parent_browser_info()})
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
