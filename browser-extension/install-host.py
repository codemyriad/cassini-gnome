#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Register this checkout's native host for one explicitly supplied extension."""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "src" if (root / "src/cassini_talk").is_dir() else root))

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("extension_id")
parser.add_argument("--browser", choices=["chromium", "google-chrome"], default="chromium")
parser.add_argument(
    "--host", help="Absolute path to cassini-browser-host (defaults to installed host or this checkout)"
)
args = parser.parse_args()
if not re.fullmatch("[a-p]{32}", args.extension_id):
    parser.error("Expected the 32-character extension ID shown in chrome://extensions")
# Meson substitutes this for installed copies; checkout use retains the token.
configured_host = Path("@bindir@") / "cassini-browser-host"
host = Path(
    args.host
    or (configured_host if configured_host.is_absolute() and configured_host.is_file() else None)
    or shutil.which("cassini-browser-host")
    or Path(__file__).resolve().with_name("native_host.py")
).resolve()
if not host.is_file():
    parser.error("Native host not found; install Cassini or specify --host")
if not os.access(host, os.X_OK):
    host.chmod(0o755)
config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def register(name, executable):
    destination = config / args.browser / "NativeMessagingHosts" / f"{name}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "name": name,
                "description": "Cassini local meeting recorder",
                "path": str(executable),
                "type": "stdio",
                "allowed_origins": [f"chrome-extension://{args.extension_id}/"],
            },
            indent=2,
        )
        + "\n"
    )
    print(destination)


register("io.codemyriad.cassini", host)
control = host.with_name("cassini-browser-control-host")
if not control.is_file():
    control = Path(__file__).resolve().with_name("control_host.py")
if control.is_file():
    register("io.codemyriad.cassini.control", control)
else:
    print("Control host not installed; browser recordings can only be started from the popup")
