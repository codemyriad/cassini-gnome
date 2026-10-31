# SPDX-License-Identifier: GPL-3.0-or-later
"""Linux native messaging host for the shared browser recording protocol."""

import logging
import subprocess
import sys
from pathlib import Path

from .recording_session import BrowserHost, ProtocolError, read_message, write_message

LOG = logging.getLogger(__name__)


def main(directory=None, open_completed=True, *, app_path=None, input_stream=None, output_stream=None, launcher=None):
    incoming = sys.stdin.buffer if input_stream is None else input_stream
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    launcher = subprocess.Popen if launcher is None else launcher
    host = BrowserHost(directory)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        independent = False
        while (message := read_message(incoming)) is not None:
            if (
                message.get("type") == "start"
                and message.get("independent") is True
                and not independent
                and host.session is None
            ):
                from .browser_audio_proxy import BrowserAudioProxy

                host = BrowserAudioProxy()
                independent = True
            result = host.handle(message)
            if result["type"] == "stopped" and open_completed and not independent:
                try:
                    launcher(
                        [str(app_path or "cassini-talk"), str(Path(result["path"]) / "session.json")],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except OSError as error:
                    LOG.warning("Recording saved at %s, but the app could not start: %s", result["path"], error)
            write_message(outgoing, result)
        host.close()
        return 0
    except Exception as error:
        code = error.code if isinstance(error, ProtocolError) else "io" if isinstance(error, OSError) else "state"
        try:
            host.close(str(error), problem_kind="io" if code == "io" else "capture")
        except Exception as cleanup_error:
            LOG.error("Failed to finalize partial recording: %s", cleanup_error)
        LOG.warning("Recording host %s error: %s", code, error)
        write_message(outgoing, {"type": "error", "code": code, "message": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
