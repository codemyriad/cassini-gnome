# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wait for the private PulseAudio socket to appear."""

import sys
from pathlib import Path

from gi.repository import Gio, GLib


def wait_for_socket(path, timeout_ms=10_000):
    loop = GLib.MainLoop()
    directory = Gio.File.new_for_path(str(path.parent))
    monitor = directory.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)

    def changed(*_args):
        if path.is_socket():
            loop.quit()

    def expired(*_args):
        loop.quit()
        return GLib.SOURCE_REMOVE

    monitor.connect("changed", changed)
    guard = GLib.timeout_source_new(timeout_ms)
    guard.set_callback(expired)
    guard.attach(loop.get_context())
    try:
        # Arm the watch first so a socket created during setup is never missed.
        if not path.is_socket():
            loop.run()
        return path.is_socket()
    finally:
        guard.destroy()
        monitor.cancel()


if __name__ == "__main__":
    path = Path(sys.argv[1])
    if not wait_for_socket(path):
        raise SystemExit(f"Private PulseAudio socket did not appear: {path}")
