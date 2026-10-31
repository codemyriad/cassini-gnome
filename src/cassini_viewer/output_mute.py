# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the mute bit of the output actually monitored; never change audio routing."""

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class OutputState:
    monitor: str
    sink: str
    muted: bool


def read_output_state(monitor):
    """Worker-only metadata query. None means unknown, never 'unmuted'.

    Matching the sink's monitor name follows the stream's actual device,
    including a non-default output. No input stream or dummy microphone is
    opened. pactl is present in GNOME Platform 50; native packages need its
    distribution package when this observer is wired into capture.
    """
    if not isinstance(monitor, str) or not monitor or len(monitor.encode()) > 4096:
        return None
    try:
        response = subprocess.run(
            ["pactl", "--format=json", "list", "sinks"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
        if len(response.stdout) > 4 * 1024 * 1024:
            return None
        sinks = json.loads(response.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(sinks, list):
        return None
    matches = [s for s in sinks if isinstance(s, dict) and s.get("monitor_source") == monitor]
    if len(matches) != 1:
        return None
    sink = matches[0]
    if not isinstance(sink.get("name"), str) or not sink["name"] or type(sink.get("mute")) is not bool:
        return None
    return OutputState(monitor, sink["name"], sink["mute"])
