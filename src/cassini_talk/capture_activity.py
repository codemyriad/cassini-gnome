# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only, bounded broker polling; capture identity comes from controllers."""

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .browser_service import read_packet, send_packet, service_path
from .capture_guard import browser_capture_busy
from .library import meeting_id

# A broker that is there but did not answer a status poll. It has not said that
# nothing records, so each cache keeps its last answer up to the bound:
# - TimeoutError: no reply within the poll's bound; a busy broker (its
#   once-a-second session checkpoint fsyncs under the capture lock that a
#   status reply also takes) answers late.
# - BlockingIOError (EAGAIN) on connect: its listen backlog is full; it still
#   listens, but its accept loop is stuck.
# - EOF, a reset or a broken pipe before the reply: it accepted the poll and
#   dropped it unanswered.
# - A malformed, partial or error reply: it spoke, but said nothing of its
#   captures.
UNANSWERED = object()
# A connect that finds no broker: its socket is gone (ENOENT; a broker that
# exits unlinks it) or nothing listens on it (ECONNREFUSED; one that crashed
# left it behind). A broker that listens, however stuck, never refuses: a full
# backlog fails with EAGAIN. This is a definite answer, not silence: no broker
# runs, so nothing records. It is stamped like a reply, and is the newest one,
# so it supersedes every older live cache at once (standing_broker_caches).
BROKER_ABSENT = (FileNotFoundError, ConnectionRefusedError)
# How long a broker's last answer outlives polls that go unanswered. A frozen
# broker that still holds its socket must not keep a meeting "recording"
# forever. The extension gives up on a broker that has not acked a packet for
# 10 s ("Cassini stopped responding", background.js) and ends its capture, so
# after as long the app stops believing in it too.
UNANSWERED_BOUND = 10.0


def broker_silent(answered_at, failed_at):
    """The one expiry rule for every cache of broker capture state.

    The app's broker snapshot (BrowserWatch, also handed to each window) and
    each Library window's BrowserActivity keep the broker's last answer while
    a poll goes unanswered: a late broker has not said that nothing records.
    That answer stands only until a poll fails UNANSWERED_BOUND or more after
    it was received; the cache then reads as idle, as an answer that nothing
    records would. Both are monotonic stamps taken on the poll's worker as the
    reply or the failure came, so a stalled main loop neither ages an answer
    nor hides the silence of a poll that blocked.
    """
    return answered_at is None or failed_at - answered_at >= UNANSWERED_BOUND


def standing_broker_caches(app, browser=None):
    """Which caches of broker capture state Library, Record and Quit read.

    The app's snapshot (with the windows' copies of it) and the Library
    windows' BrowserActivity caches hold replies to the same broker's status
    poll, each stamped with its monotonic receive time (``answered_at``). The
    newest successful reply wins: a cache whose reply is older than another's
    has been superseded and does not count, whatever order the callbacks ran
    in. A cache without a receive time (nothing answered yet) stands. Returns
    whether the app's snapshots stand, and the BrowserActivity caches that do.
    """
    sources = [s for w in app.get_windows() if (s := getattr(w, "_activity_source", None)) is not None]
    if browser is not None and all(s is not browser for s in sources):
        sources.append(browser)
    app_at = getattr(app, "_browser_answered_at", None)
    stamps = [at for at in (app_at, *(getattr(s, "answered_at", None) for s in sources)) if at is not None]
    newest = max(stamps, default=None)

    def stands(at):
        return at is None or at >= newest

    return stands(app_at), [s for s in sources if stands(getattr(s, "answered_at", None))]


def read_browser_activity(path=None, with_connection=False, with_paths=False, *, unanswered=None):
    """The broker's capture state. No broker (BROKER_ABSENT) reads as idle: an
    answer that nothing records. With `unanswered`, any other failure, from a
    broker that is there but did not answer (UNANSWERED: a timeout, a full
    backlog, an EOF, a malformed or error reply), returns `unanswered`, as
    BrowserWatch treats the same failures, so the caller keeps its last answer
    up to the bound (broker_silent). Without it, as before, every failure reads
    as idle."""
    idle = ({}, False, {}) if with_paths else (({}, False) if with_connection else {})
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.25)
            connection.connect(str(path or service_path()))
            with connection.makefile("rwb") as stream:
                send_packet(stream, dict(role="app", action="status"))
                value = read_packet(stream)
        if unanswered is not None and (not isinstance(value, dict) or value.get("error")):
            return unanswered
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
    except BROKER_ABSENT:
        return idle
    except (OSError, ValueError, TypeError, AttributeError):
        return idle if unanswered is None else unanswered


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
    app_stands, sources = standing_broker_caches(app, browser)
    activity = {}
    if browser is None:
        activity.update(fallback)
    for source in sources:
        activity.update(source.snapshot)
    activity.update(recorder_activity(app.get_windows() + [app]))
    if recovery := getattr(app, "library_recovery", None):
        activity.update(recovery.activity)
    for state in getattr(app, "_browser_snapshot", {}).get("statuses", []) if app_stands else ():
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
        # Monotonic receive time of the answer the snapshot holds (broker_silent).
        self.answered_at = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-activity")
        # GLib only here, in the poller: capture_guard and installed_build load
        # this module for broker state alone, where PyGObject may be absent.
        from gi.repository import GLib

        self.timer = GLib.timeout_add(interval_ms, self.poll)
        self.poll()

    def _read(self):
        result = read_browser_activity(self.path, True, True, unanswered=UNANSWERED)
        return self.clock(), result

    def poll(self):
        from gi.repository import GLib

        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.future is None:
            self.future = self.executor.submit(self._read)
            self.future.add_done_callback(lambda future: GLib.idle_add(self._complete, future))
        return GLib.SOURCE_CONTINUE

    def _complete(self, future):
        from gi.repository import GLib

        if not future.done():
            return False
        self.future = None
        if self.closed:
            return GLib.SOURCE_REMOVE
        # Unanswered is unknown, not idle: publishing "nothing records" for one
        # late reply unbinds a live meeting's views and tears its meter down.
        # Keep what the broker last said until it answers again or stays
        # silent for UNANSWERED_BOUND (broker_silent); then it reads as idle,
        # as a reply that nothing records would. Only a reply is stamped: a
        # failure stamped as an answer would be the newest one and hide the
        # app's live snapshot (standing_broker_caches). No broker at all is an
        # answer (BROKER_ABSENT): read_browser_activity returns idle for it,
        # stamped here like any reply, so it is the newest at once.
        at, result = future.result()
        if result is UNANSWERED:
            if not broker_silent(self.answered_at, at):
                return GLib.SOURCE_REMOVE
            result = ({}, False, {})
        elif self.answered_at is not None and at < self.answered_at:
            return GLib.SOURCE_REMOVE  # An older answer never replaces a newer one.
        else:
            self.answered_at = at
        value, connected, paths = result
        if value != self.snapshot or connected != self.connected or paths != self.paths:
            self.snapshot = value
            self.connected = connected
            self.paths = paths
            self.changed()
        return GLib.SOURCE_REMOVE

    def close(self):
        from gi.repository import GLib

        self.closed = True
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.executor.shutdown(wait=False, cancel_futures=True)
