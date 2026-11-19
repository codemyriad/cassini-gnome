# SPDX-License-Identifier: GPL-3.0-or-later
"""Open a panel of the desktop's Settings; Cassini changes nothing there.

In Flatpak, gnome-control-center is not in the sandbox: it runs on the host
through the granted Flatpak host bridge. Worker-only: the launch waits briefly
for an early exit, so a missing or failing Settings is reported, never hidden.
"""

import errno
import subprocess

from .companion import in_flatpak

SETTINGS = "gnome-control-center"


def command(panel):
    argv = [SETTINGS, panel]
    if in_flatpak():
        from .flatpak_bridge import detached_command

        # The host shell returns once Settings is up; no sandbox lifetime tie.
        return detached_command(argv, check_early_exit=True)
    return argv


def open_panel(panel, *, launch=subprocess.Popen):
    """Worker-only. Raises OSError when Settings could not be opened."""
    process = launch(
        command(panel),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # Settings stays open, or hands over to a running one and exits 0.
        result = process.wait(timeout=2.0 if in_flatpak() else 0.5)
    except subprocess.TimeoutExpired:
        return
    if result:
        raise OSError(errno.EIO, f"Settings exited with status {result}")
