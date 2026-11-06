# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic pre-identity broker; invoked as a legacy module in a private home."""

import fcntl
import json
import os
import socket
import sys
from pathlib import Path

assert os.environ.get("CASSINI_LEGACY_TEST") == "1"
assert sys.argv[1] == "--serve"
path = Path(sys.argv[2])
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
with path.with_suffix(".lock").open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen()
        print("READY", flush=True)
        try:
            while True:
                connection, _ = server.accept()
                with connection, connection.makefile("rwb") as stream:
                    line = stream.readline()
                    if not line:
                        continue  # Old clients probe by connecting and closing.
                    packet = json.loads(line)
                    if packet.get("action") == "status":
                        result = dict(statuses=[dict(recording=path.with_suffix(".busy").exists())], meetings=[])
                    else:
                        result = dict(error="Unknown action")
                    stream.write(json.dumps(result).encode() + b"\n")
                    stream.flush()
        except KeyboardInterrupt:
            pass
        finally:
            path.unlink(missing_ok=True)
            print("STOPPED", flush=True)
