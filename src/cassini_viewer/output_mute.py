# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the mute bit of the output actually monitored; never change audio routing."""

import json
import logging
import subprocess
import threading
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


def intervals(metadata):
    """Whitelist observed facts; quiet PCM and malformed metadata are not evidence."""
    details = metadata.get("recordingDetails")
    health = details.get("audioHealth") if isinstance(details, dict) else None
    rows = health.get("outputMuted") if isinstance(health, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("trackId"), str)
        and 0 < len(row["trackId"]) <= 200
        and type(row.get("startMs")) is int
        and 0 <= row["startMs"] <= 9007199254740991
        and ("endMs" not in row or type(row["endMs"]) is int and row["startMs"] <= row["endMs"] <= 9007199254740991)
    ]


def currently_muted(metadata):
    return any("endMs" not in row for row in intervals(metadata))


class MuteHistory:
    """Single observer's state. Only transitions touch disk; all other edits survive."""

    def __init__(self, directory):
        self.directory = directory
        self.active = None  # (track id, actual monitor device)

    def observe(self, track, monitor, muted, at_ms):
        from .meeting_store import LOCK, read_metadata, update_metadata

        identity = (track, monitor) if track and monitor else None
        end = self.active is not None and (identity != self.active or muted is False)
        begin = identity is not None and muted is True and (self.active is None or end)
        if not end and not begin:
            return
        with LOCK:
            current = read_metadata(self.directory)
            details = dict(current.get("recordingDetails", {}))
            health = dict(details.get("audioHealth", {}))
            rows = [dict(row) for row in health.get("outputMuted", [])]
            if end:
                for row in reversed(rows):
                    if row.get("trackId") == self.active[0] and "endMs" not in row:
                        row["endMs"] = max(row["startMs"], int(at_ms))
                        break
            if begin:
                rows.append(dict(trackId=track, startMs=max(0, int(at_ms))))
            health["outputMuted"] = rows
            details["audioHealth"] = health
            update_metadata(self.directory, recordingDetails=details)
        self.active = identity if begin else None


class OutputMuteObserver:
    """One bounded read-only worker per capture; never query Pulse on GTK or PCM."""

    def __init__(self, directory, sample, changed=lambda _: None, *, query=read_output_state):
        # sample returns (committed system track id, actual monitor, meeting ms).
        self.history, self.sample, self.changed, self.query = MuteHistory(directory), sample, changed, query
        self.closed, self.wakeup = threading.Event(), threading.Event()
        self.end_ms = 0
        self.thread = threading.Thread(target=self._run, name="cassini-output-mute", daemon=True)

    def start(self):
        self.thread.start()

    def refresh(self):
        self.wakeup.set()

    def _run(self):
        try:
            while not self.closed.is_set():
                self.wakeup.clear()
                try:
                    track, monitor, at_ms = self.sample()
                    state = self.query(monitor) if track and monitor else None
                    if self.closed.is_set():
                        break
                    self.history.observe(track, monitor, state.muted if state else None, at_ms)
                    self.changed(self.history.active is not None)
                except Exception:
                    logging.getLogger(__name__).exception("Could not observe the captured output's mute state")
                self.wakeup.wait(1)
        finally:
            try:
                self.history.observe(None, None, None, self.end_ms)
                self.changed(False)
            except Exception:
                logging.getLogger(__name__).exception("Could not finish the output mute observation")

    def close(self, at_ms):
        self.end_ms = at_ms
        self.closed.set()
        self.wakeup.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            logging.getLogger(__name__).warning("Output mute observer did not finish within its shutdown bound")


def monitor_device(branch):
    """The running pulsesrc's selected device, not today's default output."""
    source = branch.get_by_name("capture-source") if branch is not None else None
    return source.get_property("current-device") if source is not None else None
