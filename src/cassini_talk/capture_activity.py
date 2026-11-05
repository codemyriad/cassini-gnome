# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only, bounded broker polling; capture identity comes from controllers."""

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gi.repository import GLib

from .browser_service import read_packet, send_packet, service_path
from .capture_guard import browser_capture_busy
from .library import meeting_id

# A broker that accepted a status poll but did not answer within the bound.
# It has not said that nothing records: it is busy (its once-a-second session
# checkpoint fsyncs under the capture lock that a status reply also takes).
UNANSWERED = object()
# How long unanswered polls may keep the last known capture. A frozen broker
# that still holds its socket must not keep a meeting "recording" forever. The
# extension gives up on a broker that has not acked a packet for 10 s ("Cassini
# stopped responding", background.js) and ends its capture, so after as long
# the Library stops believing in it too.
UNANSWERED_BOUND = 10.0


def read_browser_activity(path=None, with_connection=False, with_paths=False, *, unanswered=None):
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.25)
            connection.connect(str(path or service_path()))
            with connection.makefile("rwb") as stream:
                send_packet(stream, dict(role="app", action="status"))
                value = read_packet(stream)
        result, paths = {}, {}
        for status in (value or {}).get("statuses", []):
            if isinstance(status, dict) and browser_capture_busy(status) and isinstance(status.get("path"), str):
                directory = Path(status["path"])
                identifier = meeting_id(directory) if directory.name.startswith("Recording-") else None
                if identifier:
                    result[identifier] = "saving" if status.get("stopping") else "recording"
                    paths[identifier] = str(directory)
        connected = bool((value or {}).get("connectedBrowsers", (value or {}).get("browsers")))
        if with_paths:
            return result, connected, paths
        return (result, connected) if with_connection else result
    except (OSError, ValueError, TypeError, AttributeError) as error:
        if unanswered is not None and isinstance(error, TimeoutError):
            return unanswered
        return ({}, False, {}) if with_paths else (({}, False) if with_connection else {})


def recorder_activity(windows):
    result = {}
    from .capture_guard import native_recorders

    recorders = {id(recorder): recorder for target in windows for recorder in native_recorders(target)}
    for recorder in recorders.values():
        if recorder.pipeline is None and not getattr(recorder, "stopping", False):
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


def application_activity(app, browser=None, fallback=()):
    """One controller projection for both immediate and periodic Library refresh."""
    activity = dict(browser.snapshot) if browser is not None else dict(fallback)
    activity.update(recorder_activity(app.get_windows() + [app]))
    if recovery := getattr(app, "library_recovery", None):
        activity.update(recovery.activity)
    for state in getattr(app, "_browser_snapshot", {}).get("statuses", []):
        if browser_capture_busy(state) and isinstance(state.get("path"), str):
            identifier = meeting_id(Path(state["path"]))
            if identifier:
                activity[identifier] = "saving" if state.get("stopping") else "recording"
    return activity


class BrowserActivity:
    def __init__(self, changed, *, path=None, interval_ms=1000, clock=time.monotonic):
        self.snapshot = {}
        self.paths = {}
        self.connected = False
        self.changed = changed
        self.path = path
        self.closed = False
        self.future = None
        self.clock = clock
        # When the current streak of unanswered polls began; None while answered.
        self.unanswered_since = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-activity")
        self.timer = GLib.timeout_add(interval_ms, self.poll)
        self.poll()

    def poll(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.future is None:
            self.future = self.executor.submit(read_browser_activity, self.path, True, True, unanswered=UNANSWERED)
            self.future.add_done_callback(lambda future: GLib.idle_add(self._complete, future))
        return GLib.SOURCE_CONTINUE

    def _complete(self, future):
        if not future.done():
            return False
        self.future = None
        if self.closed:
            return GLib.SOURCE_REMOVE
        # Unanswered is unknown, not idle: publishing "nothing records" for one
        # late reply unbinds a live meeting's views and tears its meter down.
        # Keep what the broker last said until it answers, its socket goes, or
        # it stays silent for UNANSWERED_BOUND; then it reads as idle, as a
        # reply that nothing records (or a missing broker) would.
        if (result := future.result()) is UNANSWERED:
            now = self.clock()
            if self.unanswered_since is None:
                self.unanswered_since = now
            if now - self.unanswered_since < UNANSWERED_BOUND:
                return GLib.SOURCE_REMOVE
            result = ({}, False, {})
        else:
            self.unanswered_since = None
        value, connected, paths = result
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
