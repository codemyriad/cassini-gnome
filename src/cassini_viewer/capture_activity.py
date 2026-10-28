# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only, bounded broker polling; capture identity comes from controllers."""

import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gi.repository import GLib

from .browser_service import read_packet, send_packet, service_path
from .library import meeting_id


def read_browser_activity(path=None, with_connection=False, with_paths=False):
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.25)
            connection.connect(str(path or service_path()))
            with connection.makefile("rwb") as stream:
                send_packet(stream, dict(role="app", action="status"))
                value = read_packet(stream)
        result, paths = {}, {}
        for status in (value or {}).get("statuses", []):
            if isinstance(status, dict) and status.get("recording") is True and isinstance(status.get("path"), str):
                directory = Path(status["path"])
                identifier = meeting_id(directory) if directory.name.startswith("Recording-") else None
                if identifier:
                    result[identifier] = "recording"
                    paths[identifier] = str(directory)
        connected = bool((value or {}).get("connectedBrowsers", (value or {}).get("browsers")))
        if with_paths:
            return result, connected, paths
        return (result, connected) if with_connection else result
    except (OSError, ValueError, TypeError, AttributeError):
        return ({}, False, {}) if with_paths else (({}, False) if with_connection else {})


def recorder_activity(windows):
    result = {}
    from .capture_guard import native_recorders

    recorders = {id(recorder): recorder for target in windows for recorder in native_recorders(target)}
    for recorder in recorders.values():
        if recorder.pipeline is None:
            continue
        # The current standalone Opus recorder has no Library UUID. Do not
        # invent one from its timestamp or title. Recognise controller-owned
        # session directories when present, including the future native route.
        session = getattr(recorder, "session", None)
        directory = getattr(session, "directory", None)
        if directory is None and getattr(recorder, "path", None):
            directory = Path(recorder.path).parent
        identifier = meeting_id(Path(directory)) if directory else None
        if identifier:
            result[identifier] = "saving" if recorder.stopping else "recording"
    return result


class BrowserActivity:
    def __init__(self, changed, *, path=None, interval_ms=1000):
        self.snapshot = {}
        self.paths = {}
        self.connected = False
        self.changed = changed
        self.path = path
        self.closed = False
        self.future = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-activity")
        self.timer = GLib.timeout_add(interval_ms, self.poll)
        self.poll()

    def poll(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.future is None:
            self.future = self.executor.submit(read_browser_activity, self.path, True, True)
            self.future.add_done_callback(lambda future: GLib.idle_add(self._complete, future))
        return GLib.SOURCE_CONTINUE

    def _complete(self, future):
        self.future = None
        if not self.closed:
            value, connected, paths = future.result()
            if value != self.snapshot or connected != self.connected or paths != self.paths:
                self.snapshot = value
                self.connected = connected
                self.paths = paths
                self.changed()
        return GLib.SOURCE_REMOVE

    def close(self):
        self.closed = True
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.executor.shutdown(wait=False, cancel_futures=True)
