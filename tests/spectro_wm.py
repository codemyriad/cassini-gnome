# SPDX-License-Identifier: GPL-3.0-or-later
"""A real, test-owned X11 window manager for minimize/restore coverage."""

import atexit
import os
import shutil
import subprocess
from pathlib import Path

from readiness import wait_until


class WindowManager:
    def __init__(self):
        assert os.environ.get("CASSINI_SCREENSHOT_ISOLATED") == "1" and not os.environ.get("WAYLAND_DISPLAY")
        environment = dict(os.environ)
        root = os.environ.get("CASSINI_SPECTRO_WM_ROOT")
        if root:
            root = Path(root)
            binary = str(root / "usr/bin/openbox")
            xprop = str(root / "usr/bin/xprop")
            environment.update(
                LD_LIBRARY_PATH=str(root / "usr/lib/x86_64-linux-gnu"),
                XDG_DATA_DIRS=str(root / "usr/share") + ":/usr/share",
                XDG_CONFIG_DIRS=str(root / "etc/xdg") + ":/etc/xdg",
            )
        else:
            binary = shutil.which("openbox")
            xprop = shutil.which("xprop")
        assert binary and xprop, "Install openbox and x11-utils for the private minimize/restore gate"
        self.log = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".wm.log").open("w")
        self.process = subprocess.Popen([binary, "--sm-disable"], env=environment, stdout=self.log, stderr=self.log)
        atexit.register(self.close)

        def ready():
            assert self.process.poll() is None, "Private window manager exited"
            state = subprocess.run(
                [xprop, "-root", "_NET_SUPPORTING_WM_CHECK"], capture_output=True, text=True, timeout=2
            )
            return state.returncode == 0 and "window id #" in state.stdout

        wait_until(ready, "private window manager acquired the root window", timeout=8)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(3)
        self.log.close()
