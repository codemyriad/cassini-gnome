# SPDX-License-Identifier: GPL-3.0-or-later
"""Linux native messaging host for the shared browser recording protocol."""

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from .recording_session import BrowserHost, ProtocolError, read_message, write_message

LOG = logging.getLogger(__name__)


def main(
    directory=None,
    open_completed=True,
    *,
    app_path=None,
    input_stream=None,
    output_stream=None,
    launcher=None,
    broker_closed=None,
):
    incoming = sys.stdin.buffer if input_stream is None else input_stream
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    launcher = subprocess.Popen if launcher is None else launcher
    # The broker closed this capture's transport. Exiting closes the native
    # port, so the extension tears its session down now (D52.5 needs the tab
    # released before it asks to resume), not at its next packet.
    broker_closed = (lambda: os._exit(0)) if broker_closed is None else broker_closed
    host = BrowserHost(directory)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    # Held while a message is relayed and answered: the broker's EOF never
    # cuts a reply in half, and the extension sees the reply first.
    relaying = threading.Lock()
    finished = [False]

    def closed():
        with relaying:
            if not finished[0]:
                broker_closed()

    try:
        independent = False
        while (message := read_message(incoming)) is not None:
            with relaying:
                kind = message.get("type")
                if (
                    kind in ("start", "resume")
                    and message.get("independent") is True
                    and not independent
                    and host.session is None
                ):
                    from .browser_audio_proxy import BrowserAudioProxy

                    try:
                        # D52.3c: a resume joins a capture the broker owns; it
                        # never spawns one, so no broker is no target.
                        proxy = BrowserAudioProxy(spawn=kind == "start")
                    except OSError:
                        if kind != "resume":
                            raise
                        write_message(
                            outgoing,
                            dict(type="error", code="state", scope="command", message="Target is no longer active."),
                        )
                        finished[0] = True
                        return 0
                    host = proxy
                    independent = True
                    host.watch(closed)
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
        with relaying:
            finished[0] = True
        host.close()
        return 0
    except Exception as error:
        with relaying:
            finished[0] = True
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
