# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the mute bit of the output actually monitored; never change audio routing."""

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import NamedTuple


@dataclass(frozen=True)
class OutputState:
    monitor: str
    sink: str
    muted: bool


class OutputSample(NamedTuple):
    track: str | None
    monitor: str | None
    at_ms: int
    token: object


def read_output_state(monitor):
    """Worker-only metadata query. None means unknown, never 'unmuted'.

    Matching the sink's monitor name follows the stream's actual device,
    including a non-default output. No input stream or dummy microphone is
    opened. pactl is present in GNOME Platform 50; native packages need its
    distribution package when this observer is wired into capture.
    """
    if not isinstance(monitor, str) or not monitor or len(monitor.encode()) > 4096:
        return None
    sinks = _pulse_list("sinks")
    if not isinstance(sinks, list):
        return None
    matches = [s for s in sinks if isinstance(s, dict) and s.get("monitor_source") == monitor]
    if len(matches) != 1:
        return None
    sink = matches[0]
    if not isinstance(sink.get("name"), str) or not sink["name"] or type(sink.get("mute")) is not bool:
        return None
    return OutputState(monitor, sink["name"], sink["mute"])


def _pulse_list(kind=None):
    """A bounded child owns its Pulse context; none is shared with GStreamer."""
    command = ["pactl", "--format=json", "list"]
    if kind is not None:
        command.append(kind)
    try:
        response = subprocess.run(command, check=True, capture_output=True, text=True, timeout=1)
        if len(response.stdout) > 4 * 1024 * 1024:
            return None
        return json.loads(response.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


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
        from .library_edits import mutation as transaction
        from .meeting_store import read_metadata, update_metadata

        identity = (track, monitor) if track and monitor else None
        end = self.active is not None and (identity != self.active or muted is False)
        begin = identity is not None and muted is True and (self.active is None or end)
        if not end and not begin:
            return
        with transaction(self.directory):
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

    def __init__(self, directory, sample, changed=lambda _: None, *, query=read_output_state, validate=None):
        # Query without the capture lock, then revalidate the committed source
        # and timestamp after both Pulse queries finish. Tokens stay opaque here.
        self.history, self.sample, self.changed, self.query = MuteHistory(directory), sample, changed, query
        self.validate = validate or (lambda value: value[2])
        self.closed, self.wakeup = threading.Event(), threading.Event()
        self._retire_lock = threading.Lock()
        self._retired, self._retirements = set(), []
        self.end_ms = 0
        self.thread = threading.Thread(target=self._run, name="cassini-output-mute", daemon=True)

    def start(self):
        self.thread.start()

    def refresh(self):
        self.wakeup.set()

    def retire(self, track, at_ms):
        """Queue the committed boundary without querying Pulse or writing on PCM/GTK."""
        with self._retire_lock:
            self._retired.add(track)
            self._retirements.append((track, at_ms))
        self.wakeup.set()

    def _drain_retirements(self):
        with self._retire_lock:
            pending, self._retirements = self._retirements, []
        for track, at_ms in pending:
            if self.history.active is not None and self.history.active[0] == track:
                self.history.observe(None, None, None, at_ms)
                self.changed(False)

    def _run(self):
        try:
            while not self.closed.is_set():
                self.wakeup.clear()
                try:
                    self._drain_retirements()
                    sample = self.sample()
                    track, monitor = sample[:2]
                    state = self.query(monitor) if track and monitor else None
                    self._drain_retirements()
                    if self.closed.is_set():
                        break
                    at_ms = self.validate(sample)
                    with self._retire_lock:
                        retired = track in self._retired
                    # A missing source is closed by its committed retirement,
                    # never by the timestamp of a later polling iteration.
                    if track and not retired and at_ms is not None:
                        self.history.observe(track, monitor, state.muted if state else None, at_ms)
                        self.changed(self.history.active is not None)
                except Exception:
                    logging.getLogger(__name__).exception("Could not observe the captured output's mute state")
                self.wakeup.wait(1)
        finally:
            try:
                self._drain_retirements()
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
    """Resolve the live stream externally, never through pulsesrc.current-device.

    That getter queries pulsesrc's pa_context after dropping its mainloop lock;
    concurrent NULL/close can clear it and libpulse aborts on the NULL context.
    source-output-index only copies a cached integer. A separate pactl process
    owns and tears down its own context, so source teardown cannot invalidate
    the query. Recheck the cached index after IO; retirement/reconnect is unknown.
    """
    source = branch.get_by_name("capture-source") if branch is not None else None
    if source is None:
        return None
    index = source.get_property("source-output-index")
    if type(index) is not int or not 0 <= index < 0xFFFFFFFF:
        return None
    snapshot = _pulse_list()
    if not isinstance(snapshot, dict):
        return None
    outputs, sources = snapshot.get("source_outputs"), snapshot.get("sources")
    if not isinstance(outputs, list) or not isinstance(sources, list):
        return None
    streams = [
        row for row in outputs if isinstance(row, dict) and type(row.get("index")) is int and row["index"] == index
    ]
    if len(streams) != 1:
        return None
    stream = streams[0]
    properties = stream.get("properties")
    if not isinstance(properties, dict) or properties.get("application.process.id") != str(os.getpid()):
        return None  # A restarted server may have reused another client's index.
    device = stream.get("source")
    if type(device) is not int:
        return None
    matches = [
        row for row in sources if isinstance(row, dict) and type(row.get("index")) is int and row["index"] == device
    ]
    if len(matches) != 1 or source.get_property("source-output-index") != index:
        return None
    name = matches[0].get("name")
    return name if isinstance(name, str) and name and len(name.encode()) <= 4096 else None
