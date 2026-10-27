# SPDX-License-Identifier: GPL-3.0-or-later
"""Linux native messaging host for the shared browser recording protocol."""

import contextlib
import subprocess
import sys
from pathlib import Path

from .recording_session import BrowserHost, read_message, write_message


def main(directory=None, open_completed=True):
    host = BrowserHost(directory)
    try:
        while (message := read_message(sys.stdin.buffer)) is not None:
            result = host.handle(message)
            if result["type"] == "stopped" and open_completed:
                with contextlib.suppress(OSError):
                    subprocess.Popen(
                        ["cassini-viewer", str(Path(result["path"]) / "session.json")],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
            write_message(sys.stdout.buffer, result)
            if result["type"] == "stopped":
                break
    except Exception as error:
        try:
            host.close(str(error))
        finally:
            write_message(sys.stdout.buffer, {"type": "error", "message": str(error)})
        return 1
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
