# SPDX-License-Identifier: GPL-3.0-or-later
"""Local meeting inventory and commands shared by the app and browser hosts.

The Unix socket is private to this user. Only metadata and control messages use
it; audio continues over the existing, independently validated recording host.
"""

import argparse
import contextlib
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

MAX_PACKET = 512 * 1024


class StartRejected(ValueError):
    """The extension finished rejecting Start, rather than an unknown/late result."""


def service_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    parent = Path(runtime) if runtime else Path("/tmp") / f"cassini-{os.getuid()}"
    return parent / "cassini" / "browser-v2.sock"


def read_packet(stream):
    line = stream.readline(MAX_PACKET + 1)
    if not line:
        return None
    if len(line) > MAX_PACKET or not line.endswith(b"\n"):
        raise ValueError("Invalid control packet size")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Expected a control object")
    return value


def send_packet(stream, value):
    data = json.dumps(value, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_PACKET:
        raise ValueError("Control packet is too large")
    stream.write(data)
    stream.flush()


def connect(path=None, *, timeout=30):
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(timeout)
    connection.connect(str(path or service_path()))
    return connection


def ensure_service(path=None, *, cancelled=None):
    target = Path(path or service_path())
    try:
        connection = connect(target, timeout=0.5)
        connection.close()
        return target
    except OSError:
        pass
    if cancelled is not None and cancelled.is_set():
        raise CancelledError()
    # A filesystem lock in serve() elects one process if browser and app start together.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    subprocess.Popen(
        [sys.executable, "-m", "cassini_viewer.browser_service", "--serve", str(target)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            raise CancelledError()
        try:
            connection = connect(target, timeout=0.5)
            connection.close()
            return target
        except OSError:
            time.sleep(0.05)
    raise OSError("Could not start the browser connection service")


def request(action="status", *, path=None, **values):
    with connect(path) as connection, connection.makefile("rwb") as stream:
        send_packet(stream, dict(role="app", action=action, **values))
        response = read_packet(stream)
        if response is None:
            raise OSError("Browser connection service closed")
        if response.get("error"):
            raise ValueError(response["error"])
        return response


class BrowserConnection:
    def __init__(self, stream, browser_name=None):
        self.browser_name = browser_name if isinstance(browser_name, str) and len(browser_name) <= 100 else None
        self.id = str(uuid4())
        self.stream = stream
        self.write_lock = threading.Lock()
        self.meetings = {}
        self.status = {"recording": False}
        self.status_seen = self.hello_seen = False
        self.notifications = {}
        self.pending = {}
        self.attempted = set()
        self.error = None
        self.busy = set()
        self.scheduled = set()
        self.missing_since = {}
        self.states = {}
        self.capabilities = set()
        self.closed = False

    def send(self, packet):
        with self.write_lock:
            send_packet(self.stream, packet)


class BrowserService:
    def __init__(
        self,
        config=None,
        *,
        command_timeout=25,
        notifier=None,
        capture_parent=None,
        tap_factory=None,
        require_application=False,
    ):
        self.config = (
            Path(config)
            if config
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cassini/browser.json"
        )
        self.auto_record = False
        self.auto_record_revision = time.monotonic_ns()
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.auto_record = json.loads(self.config.read_text()).get("autoRecord") is True
        self.lock = threading.RLock()
        self.browsers = {}
        self.recordings = []
        self.captures = {}
        self.capture_parent, self.tap_factory = capture_parent, tap_factory
        self.applications = {}
        self.application_pids = {}
        self.paused_pids = {}
        self.application_generations = {}
        self.require_application = require_application
        self._stopping_all = threading.Event()
        self._stop_generation = 0
        self._record_groups = {}
        self._cancelled_groups = {}
        self.command_timeout = command_timeout
        if notifier is None:
            from .browser_notifications import notify_recording

            notifier = notify_recording
        self.notifier = notifier
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cassini-browser")
        self.closed = threading.Event()

    def snapshot(self):
        # Never hold the inventory lock while waiting for a writer lock: PCM
        # and finalization report changes in the opposite direction.
        with self.lock:
            captures = list(self.captures.values())
        owned = {c.capture_id: c.snapshot() for c in captures if c.directory is not None}
        with self.lock:
            meetings, statuses, seen = [], [], set()
            for browser in self.browsers.values():
                for tab, value in browser.states.items():
                    value = dict(value)
                    capture = owned.get(value.get("captureId"))
                    if capture is not None:
                        value.update(capture)
                        seen.add(capture["captureId"])
                    value["id"] = f"{browser.id}:{tab}"
                    statuses.append(value)
                for meeting in browser.meetings.values():
                    value = next((v for v in statuses if v["id"] == f"{browser.id}:{meeting['tabId']}"), {})
                    meetings.append(
                        dict(
                            meeting,
                            id=f"{browser.id}:{meeting['tabId']}",
                            recording=bool(value.get("recording")),
                            path=value.get("path"),
                            busy=meeting["tabId"] in browser.busy,
                            browserRecording=any(v.get("recording") for v in browser.states.values()),
                            error=browser.error,
                        )
                    )
            for identifier, value in owned.items():
                if identifier not in seen:
                    statuses.append(dict(value, id="capture:" + identifier))
            return dict(
                browsers=len(self.browsers),
                protocol=2,
                connectedBrowsers=sum(b.hello_seen and b.status_seen for b in self.browsers.values()),
                browserNames=sorted(
                    {
                        b.browser_name
                        for b in self.browsers.values()
                        if b.browser_name and b.hello_seen and b.status_seen
                    }
                ),
                autoRecord=self.auto_record,
                autoRecordRevision=self.auto_record_revision,
                meetings=meetings,
                recordings=list(self.recordings),
                statuses=statuses,
            )

    def _capture_changed(self, capture, event):
        if event == "error":
            self.workers.submit(capture.stop, capture.error)
        elif event == "finished" and capture.directory is not None:
            self._remember_saved(str(capture.directory), capture.host.session.manifest["title"])

    def _remember_saved(self, path, title, message=None):
        with self.lock:
            recording = dict(path=path, title=title, message=message or "Recording saved")
            self.recordings = [recording] + [r for r in self.recordings if r["path"] != path][:19]

    def _audio(self, stream):
        from .browser_capture import BrowserCapture
        from .recording_session import ProtocolError

        capture = None
        problem = None
        send_packet(stream, dict(ready=True, protocol=2))
        try:
            while (packet := read_packet(stream)) is not None:
                try:
                    if capture is None:
                        if packet.get("type") != "start" or packet.get("independent") is not True:
                            raise ProtocolError("state", "Expected an independent recording start")
                        identifier = packet.get("captureId")
                        if not isinstance(identifier, str) or not identifier:
                            raise ProtocolError("state", "Invalid independent capture identity")
                        with self.lock:
                            if identifier in self.captures:
                                raise ProtocolError("state", "A capture with this identity already exists")
                            capture = BrowserCapture(
                                self._capture_changed, parent=self.capture_parent, tap_factory=self.tap_factory
                            )
                            # Reserve identity before startup. Snapshots ignore a
                            # reservation until it has created its own folder.
                            self.captures[identifier] = capture
                        result = capture.start(packet)
                    else:
                        result = capture.handle(packet)
                    send_packet(stream, result)
                except Exception as error:
                    problem = str(error)
                    code = (
                        error.code
                        if isinstance(error, ProtocolError)
                        else "io"
                        if isinstance(error, OSError)
                        else "state"
                    )
                    send_packet(stream, dict(type="error", code=code, message=str(error)))
                    break
        finally:
            if capture is not None:
                try:
                    if problem or self.closed.is_set():
                        capture.stop(problem)
                    else:
                        capture.close()
                finally:
                    with self.lock:
                        if capture.directory is None:
                            self.captures = {key: c for key, c in self.captures.items() if c is not capture}
                        finished = [key for key, c in self.captures.items() if c.finished]
                        for key in finished[:-19]:
                            self.captures.pop(key, None)

    def configure(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Expected an automatic recording setting")
        with self.lock:
            self.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.config.with_suffix(".tmp")
            temporary.write_text(json.dumps({"autoRecord": enabled}))
            temporary.chmod(0o600)
            temporary.replace(self.config)
            self.auto_record = enabled
            self.auto_record_revision = max(self.auto_record_revision + 1, time.monotonic_ns())
            if enabled:
                for browser in self.browsers.values():
                    browser.attempted.clear()
        self.policy()
        return self.snapshot()

    def _status(self, browser, value):
        if type(value.get("recording")) is not bool:
            return
        browser.status_seen = True
        previous = browser.states
        browser.status = {
            key: value[key] for key in ("recording", "tabId", "path", "message", "tracks") if key in value
        }
        if "sessions" in value:
            items = value["sessions"]
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError("Invalid recording inventory")
            states = {}
            for item in items:
                if (
                    not isinstance(item, dict)
                    or type(item.get("tabId")) is not int
                    or type(item.get("recording")) is not bool
                ):
                    raise ValueError("Invalid recording status")
                tab = item["tabId"]
                if tab in states or not 0 <= tab <= 9007199254740991:
                    raise ValueError("Invalid or duplicate recording tab")
                states[tab] = {
                    key: item[key]
                    for key in ("recording", "tabId", "path", "message", "tracks", "stopping", "captureId")
                    if key in item
                }
        else:
            tab = value.get("tabId")
            states = {tab: dict(browser.status)} if value["recording"] else {}
        interrupted = value.get("interrupted", [])
        if isinstance(interrupted, list):
            for item in interrupted[:100]:
                if not isinstance(item, dict) or type(item.get("tabId")) is not int:
                    continue
                owner = self.captures.get(item.get("captureId"))
                if owner is not None and not owner.finished and str(owner.directory) == item.get("path"):
                    states.setdefault(item["tabId"], dict(item, recording=True))
                    browser.attempted.add(item["tabId"])
        for tab, old in previous.items():
            owner = self.captures.get(old.get("captureId"))
            if owner is not None and not owner.finished and owner.browser_lost:
                states.setdefault(tab, old)
        browser.states = states
        for tab, old in previous.items():
            if old.get("recording") and not states.get(tab, {}).get("recording"):
                close = browser.notifications.pop(tab, None)
                if close:
                    self.workers.submit(self._close_notification, close)
                path = old.get("path") or value.get("path")
                owner = self.captures.get(old.get("captureId"))
                if isinstance(path, str) and (owner is None or owner.finished):
                    self._remember_saved(
                        path, browser.meetings.get(tab, {}).get("title", "Recorded meeting"), value.get("message")
                    )

    def update(self, browser, packet):
        with self.lock:
            kind = packet.get("type")
            if kind == "meetings":
                values = packet.get("meetings")
                if not isinstance(values, list) or len(values) > 100:
                    raise ValueError("Invalid meeting inventory")
                meetings = {}
                for value in values:
                    if (
                        not isinstance(value, dict)
                        or type(value.get("tabId")) is not int
                        or type(value.get("sourceCount")) is not int
                        or not 0 <= value["sourceCount"] <= 4096
                        or not isinstance(value.get("title"), str)
                        or not isinstance(value.get("url"), str)
                        or not value["url"].startswith(("http://", "https://"))
                    ):
                        continue
                    meetings[value["tabId"]] = {key: value[key] for key in ("tabId", "title", "url", "sourceCount")}
                    meetings[value["tabId"]]["title"] = value["title"][:200]
                    meetings[value["tabId"]]["url"] = value["url"][:2048]
                browser.meetings = meetings
                browser.attempted.intersection_update(meetings)
            elif kind == "status":
                self._status(browser, packet)
            elif kind == "result":
                pending = browser.pending.get(packet.get("requestId"))
                if pending:
                    pending["result"] = packet
                    if isinstance(packet.get("status"), dict):
                        self._status(browser, packet["status"])
                    pending["event"].set()
            elif kind == "hello":
                if type(packet.get("version")) is not int or packet["version"] != 1:
                    raise ValueError("Unsupported browser control version")
                browser.hello_seen = True
                capabilities = packet.get("capabilities", [])
                browser.capabilities = (
                    {c for c in capabilities if isinstance(c, str)}
                    if isinstance(capabilities, list) and len(capabilities) <= 16
                    else set()
                )
            else:
                raise ValueError("Unknown browser control event")
        self.policy()

    def command(self, identifier, action, *, microphone=True, preserve_capture=False):
        if action not in ("start", "stop") or not isinstance(identifier, str) or type(microphone) is not bool:
            raise ValueError("Invalid meeting command")
        if identifier.startswith("capture:"):
            with self.lock:
                capture = self.captures.get(identifier.removeprefix("capture:"))
            if action != "stop" or capture is None:
                raise ValueError("This meeting is no longer available")
            capture.stop()
            return self.snapshot()
        browser_id, _, raw_tab = identifier.partition(":")
        with self.lock:
            browser = self.browsers.get(browser_id)
            if browser is None or not raw_tab.isdecimal():
                raise ValueError("This browser is no longer connected")
            tab = int(raw_tab)
            if action == "start" and self._stopping_all.is_set():
                raise ValueError("Recording start was cancelled")
            independent = "independent-sessions-v1" in browser.capabilities
            if action == "start" and tab not in browser.meetings:
                raise ValueError("This meeting is no longer available")
            pending_start = any(p.get("action") == "start" and p["tabId"] == tab for p in browser.pending.values())
            if action == "stop":
                for pending in browser.pending.values():
                    if pending.get("action") == "start" and pending["tabId"] == tab:
                        pending["cancelled"] = True
            if (tab in browser.busy or not independent and browser.busy) and not (action == "stop" and pending_start):
                raise ValueError("A recording command is still being processed")
            if (
                action == "stop"
                and not browser.states.get(tab, {}).get("recording")
                and not independent
                and not pending_start
            ):
                raise ValueError("This meeting is not recording")
            browser.busy.add(tab)
            browser.error = None
            browser.attempted.add(tab)
            request_id = str(uuid4())
            pending = dict(event=threading.Event(), result=None, action=action, tabId=tab)
            browser.pending[request_id] = pending
            capture = self.captures.get(browser.states.get(tab, {}).get("captureId"))
            if capture is not None and action == "stop" and not preserve_capture:
                capture.stop_requested.set()
        try:
            if capture is not None and capture.browser_lost and action == "stop" and not preserve_capture:
                capture.stop()
                return dict(self.snapshot(), stoppedPath=str(capture.directory))
            packet = dict(type="command", requestId=request_id, action=action, tabId=tab, mode="tracks")
            if independent:
                packet.update(independent=True, captureLocal=False, microphone=microphone)
            browser.send(packet)
            if not pending["event"].wait(self.command_timeout):
                raise TimeoutError("The browser did not finish the recording command")
            result = pending["result"]
            if result is None or result.get("ok") is not True:
                error = (
                    StartRejected
                    if result is not None and action == "start" and not pending.get("cancelled")
                    else ValueError
                )
                raise error(str((result or {}).get("error", "The browser disconnected"))[:500])
            snapshot = self.snapshot()
            if action == "stop":
                snapshot["stoppedPath"] = (result.get("status") or {}).get("path")
            return snapshot
        except (OSError, ValueError, TimeoutError) as error:
            with self.lock:
                browser.error = str(error)
            raise
        finally:
            with self.lock:
                browser.pending.pop(request_id, None)
                if not any(p["tabId"] == tab for p in browser.pending.values()):
                    browser.busy.discard(tab)

    def start_all(self, *, microphone=True, group_id=None):
        if type(microphone) is not bool:
            raise ValueError("Invalid microphone selection")
        group = threading.Event()
        token = str(uuid4()) if group_id is None else group_id
        if not isinstance(token, str) or not 0 < len(token) <= 128:
            raise ValueError("Invalid recording operation")
        with self.lock:
            if token in self._cancelled_groups:
                group.set()
            targets = [
                (f"{b.id}:{tab}", dict(meeting))
                for b in self.browsers.values()
                if b.status_seen
                for tab, meeting in b.meetings.items()
                if meeting.get("sourceCount", 0) > 0 and not b.states.get(tab, {}).get("recording")
            ]
            self._record_groups[token] = group
        failures = []
        try:
            for identifier, meeting in targets:
                if group.is_set() or self._stopping_all.is_set():
                    break
                try:
                    self.command(identifier, "start", microphone=microphone)
                except StartRejected:
                    if not group.is_set() and not self._stopping_all.is_set():
                        try:
                            self.start_fallback(identifier, meeting, microphone, cancelled=group)
                        except (OSError, ValueError, TimeoutError) as fallback_error:
                            failures.append(dict(id=identifier, title=meeting["title"], error=str(fallback_error)))
                except (OSError, ValueError, TimeoutError) as error:
                    failures.append(dict(id=identifier, title=meeting["title"], error=str(error)))
            return dict(self.snapshot(), attempted=len(targets), cancelled=group.is_set(), failures=failures)
        finally:
            with self.lock:
                self._record_groups.pop(token, None)

    def start_fallback(self, identifier, meeting, microphone=True, *, cancelled=None, automatic_generation=None):
        from .browser_capture import BrowserCapture

        browser_id, _, raw_tab = identifier.partition(":")
        with self.lock:
            browser = self.browsers.get(browser_id)
            if (
                browser is None
                or self._stopping_all.is_set()
                or (cancelled is not None and cancelled.is_set())
                or (
                    automatic_generation is not None
                    and (
                        automatic_generation != self._stop_generation
                        or not self.auto_record
                        or self.require_application
                        and not any(self.applications.values())
                    )
                )
            ):
                raise ValueError("Recording start was cancelled")
            tab = int(raw_tab)
            existing = self.captures.get(browser.states.get(tab, {}).get("captureId"))
            if existing is None or existing.finished:
                capture_id = str(uuid4())
                capture = BrowserCapture(
                    self._capture_changed, parent=self.capture_parent, tap_factory=self.tap_factory
                )
                capture.browser_lost = True
                self.captures[capture_id] = capture
                browser.states[tab] = dict(recording=True, captureId=capture_id, tabId=tab)
            else:
                capture = None
        if capture is None:
            return existing.snapshot()
        try:
            capture.start(
                dict(
                    type="start",
                    version=1,
                    captureId=capture_id,
                    timelineOriginMs=int(time.time() * 1000),
                    microphone=microphone,
                    title=meeting["title"],
                ),
                others_source="system",
                reason="missing",
            )
        except Exception as error:
            capture.stop(str(error))
            with self.lock:
                if browser.states.get(tab, {}).get("captureId") == capture_id:
                    browser.states[tab] = dict(recording=False, tabId=tab)
            raise
        return capture.snapshot()

    def stop_all(self, directory=None, *, cancel_groups=(), quitting_pid=None, quitting_generation=0):
        if type(quitting_generation) is not int or quitting_generation < 0:
            raise ValueError("Invalid application generation")
        if (
            not isinstance(cancel_groups, (list, tuple))
            or len(cancel_groups) > 100
            or any(not isinstance(g, str) or not 0 < len(g) <= 128 for g in cancel_groups)
        ):
            raise ValueError("Invalid recording operations")

        def matches(path):
            return isinstance(path, str) and (directory is None or Path(path).resolve() == Path(directory).resolve())

        if directory is None:
            self._stopping_all.set()
            with self.lock:
                self._stop_generation += 1
                if quitting_pid is not None:
                    self.paused_pids[quitting_pid] = max(self.paused_pids.get(quitting_pid, -1), quitting_generation)
                    for key, pid in self.application_pids.items():
                        if pid == quitting_pid and self.application_generations[key] <= quitting_generation:
                            self.applications[key] = False
                for token, group in self._record_groups.items():
                    group.set()
                    self._cancelled_groups[token] = True
                for token in cancel_groups:
                    self._cancelled_groups[token] = True
                for token in list(self._cancelled_groups)[:-1024]:
                    if token not in self._record_groups:
                        self._cancelled_groups.pop(token, None)
        try:
            snapshot = self.snapshot()
            paths = [s["path"] for s in snapshot["statuses"] if s.get("recording") and matches(s.get("path"))]
            with self.lock:
                commands = {s["id"] for s in snapshot["statuses"] if s.get("recording") and matches(s.get("path"))}
                if directory is None:
                    commands.update(
                        f"{b.id}:{p['tabId']}"
                        for b in self.browsers.values()
                        for p in b.pending.values()
                        if p.get("action") == "start"
                    )
            for identifier in commands:
                try:
                    result = self.command(identifier, "stop")
                    if isinstance(result.get("stoppedPath"), str):
                        paths.append(result["stoppedPath"])
                except ValueError:
                    current = self.snapshot()
                    if any(s.get("recording") and s["id"] == identifier for s in current["statuses"]):
                        raise
            with self.lock:
                captures = [
                    c
                    for c in self.captures.values()
                    if not c.finished and (directory is None or c.directory is not None and matches(str(c.directory)))
                ]
            for capture in captures:
                capture.stop()
            return dict(self.snapshot(), stoppedPaths=list(dict.fromkeys(paths)))
        finally:
            if directory is None:
                self._stopping_all.clear()

    def policy(self):
        if self.closed.is_set() or self._stopping_all.is_set():
            return
        tasks = []
        fallbacks = []
        with self.lock:
            generation = self._stop_generation
            for browser in self.browsers.values():
                if browser.closed or not browser.status_seen:
                    continue
                independent = "independent-sessions-v1" in browser.capabilities
                for tab, state in browser.states.items():
                    if not state.get("recording") or tab in browser.busy or tab in browser.scheduled:
                        continue
                    if browser.meetings.get(tab, {}).get("sourceCount", 0) > 0:
                        browser.missing_since.pop(tab, None)
                    elif (capture := self.captures.get(state.get("captureId"))) is not None:
                        if not capture.browser_lost:
                            browser.scheduled.add(tab)
                            fallbacks.append((f"{browser.id}:{tab}", capture))
                    elif tab not in browser.missing_since:
                        browser.missing_since[tab] = time.monotonic()
                    elif time.monotonic() - browser.missing_since[tab] >= 2:
                        browser.scheduled.add(tab)
                        tasks.append((f"{browser.id}:{tab}", "stop"))
                        browser.missing_since[tab] = float("inf")
                if (
                    self.auto_record
                    and (not self.require_application or any(self.applications.values()))
                    and (
                        independent
                        or not browser.busy
                        and not browser.scheduled
                        and not any(s.get("recording") for s in browser.states.values())
                    )
                ):
                    for tab, meeting in browser.meetings.items():
                        if (
                            tab not in browser.attempted
                            and meeting["sourceCount"] > 0
                            and not browser.states.get(tab, {}).get("recording")
                        ):
                            browser.attempted.add(tab)
                            browser.scheduled.add(tab)
                            tasks.append((f"{browser.id}:{tab}", "start"))
                            if not independent:
                                break
        for identifier, action in tasks:
            self.workers.submit(self._automatic, identifier, action, generation)
        for identifier, capture in fallbacks:
            self.workers.submit(self._fallback, identifier, capture)

    def _fallback(self, identifier, capture):
        try:
            capture.fallback()
            self.command(identifier, "stop", preserve_capture=True)
        except (OSError, ValueError, TimeoutError) as error:
            logging.getLogger(__name__).warning("Browser source fallback: %s", error)
        finally:
            with self.lock:
                browser_id, _, tab = identifier.partition(":")
                if browser_id in self.browsers:
                    self.browsers[browser_id].scheduled.discard(int(tab))

    def _automatic(self, identifier, action, generation=None):
        try:
            if action == "start":
                with self.lock:
                    if (
                        self._stopping_all.is_set()
                        or (generation is not None and generation != self._stop_generation)
                        or self.require_application
                        and not any(self.applications.values())
                    ):
                        return
            self.command(identifier, action)
            if action == "start":
                if self.require_application:
                    # The application owns the single live hidden notification
                    # and its global Stop action, independent of window lifetime.
                    return
                browser_id, _, tab = identifier.partition(":")
                with self.lock:
                    browser = self.browsers.get(browser_id)
                    if browser is None or not browser.states.get(int(tab), {}).get("recording"):
                        return
                    title = browser.meetings.get(int(tab), {}).get("title", "Browser meeting")
                try:
                    close = self.notifier(title, lambda: self.workers.submit(self.stop_all))
                    with self.lock:
                        if browser.states.get(int(tab), {}).get("recording"):
                            browser.notifications[int(tab)] = close
                        elif close:
                            self.workers.submit(self._close_notification, close)
                except Exception as error:
                    logging.getLogger(__name__).warning("Could not show recording notification: %s", error)
        except StartRejected:
            browser_id, _, tab = identifier.partition(":")
            with self.lock:
                browser = self.browsers.get(browser_id)
                meeting = browser.meetings.get(int(tab)) if browser else None
                active = not self.require_application or any(self.applications.values())
                allowed = self.auto_record and active and not self._stopping_all.is_set()
            if action == "start" and allowed and meeting is not None:
                try:
                    self.start_fallback(identifier, meeting, automatic_generation=generation)
                except (OSError, ValueError, TimeoutError):
                    logging.getLogger(__name__).exception("Could not start the missing-browser fallback")
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            with self.lock:
                browser_id, _, tab = identifier.partition(":")
                if browser_id in self.browsers:
                    self.browsers[browser_id].scheduled.discard(int(tab))
            self.policy()

    @staticmethod
    def _close_notification(close):
        try:
            close()
        except Exception as error:
            logging.getLogger(__name__).warning("Could not close recording notification: %s", error)

    def _application(self, connection, stream, active, generation):
        if type(generation) is not int or generation < 0:
            raise ValueError("Invalid application generation")
        identifier = str(uuid4())
        pid, _, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try:
            with self.lock:
                self.application_pids[identifier] = pid
                self.application_generations[identifier] = generation
                self.applications[identifier] = active is True and generation > self.paused_pids.get(pid, -1)
            send_packet(stream, dict(ready=True, protocol=2))
            self.policy()
            while (packet := read_packet(stream)) is not None:
                revision = packet.get("generation", 0)
                if (
                    type(packet.get("active")) is bool
                    and type(revision) is int
                    and revision >= self.application_generations[identifier]
                ):
                    with self.lock:
                        self.application_generations[identifier] = revision
                        self.applications[identifier] = packet["active"] and revision > self.paused_pids.get(pid, -1)
                    self.policy()
        finally:
            with self.lock:
                self.applications.pop(identifier, None)
                self.application_pids.pop(identifier, None)
                self.application_generations.pop(identifier, None)

    def handle(self, connection):
        browser = None
        try:
            with connection, connection.makefile("rwb") as stream:
                packet = read_packet(stream)
                if not packet:
                    return
                if packet.get("role") == "extension":
                    browser = BrowserConnection(stream, packet.get("browserName"))
                    with self.lock:
                        self.browsers[browser.id] = browser
                    while (packet := read_packet(stream)) is not None:
                        self.update(browser, packet)
                elif packet.get("role") == "application":
                    self._application(connection, stream, packet.get("active", True), packet.get("generation", 0))
                elif packet.get("role") == "audio":
                    self._audio(stream)
                elif packet.get("role") == "app":
                    try:
                        action = packet.get("action")
                        if action == "status":
                            result = self.snapshot()
                        elif action == "start-all":
                            result = self.start_all(
                                microphone=packet.get("microphone", True), group_id=packet.get("groupId")
                            )
                        elif action == "stop-all":
                            pid, _, _ = struct.unpack(
                                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                            )
                            result = self.stop_all(
                                packet.get("directory"),
                                cancel_groups=packet.get("cancelGroups", []),
                                quitting_pid=pid if packet.get("quitting") is True else None,
                                quitting_generation=packet.get("generation", 0),
                            )
                        elif action == "configure":
                            result = self.configure(packet.get("autoRecord"))
                        elif action == "selection":
                            directory = packet.get("directory")
                            with self.lock:
                                capture = next(
                                    (
                                        c
                                        for c in self.captures.values()
                                        if c.directory is not None and str(c.directory) == directory
                                    ),
                                    None,
                                )
                            if capture is None:
                                raise ValueError("This meeting is no longer recording")
                            result = capture.select(packet.get("me"), packet.get("others"))
                        else:
                            result = self.command(packet.get("id"), action, microphone=packet.get("microphone", True))
                    except (OSError, ValueError, TimeoutError) as error:
                        result = {"error": str(error)}
                    send_packet(stream, result)
        except (OSError, ValueError):
            pass
        finally:
            if browser:
                with self.lock:
                    browser.closed = True
                    self.browsers.pop(browser.id, None)
                    for pending in browser.pending.values():
                        pending["event"].set()

    def serve(self, path, ready=None):
        import fcntl

        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
            raise PermissionError("Browser service directory must be private to this user")
        with path.with_suffix(".lock").open("w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            path.unlink(missing_ok=True)
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                path.chmod(0o600)
                server.listen(16)
                server.settimeout(0.5)
                if ready:
                    ready.set()
                try:
                    while not self.closed.is_set() and path.parent.is_dir():
                        try:
                            connection, _ = server.accept()
                            _, uid, _ = struct.unpack(
                                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                            )
                            if uid != os.getuid():
                                connection.close()
                                continue
                            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()
                        except TimeoutError:
                            self.policy()
                finally:
                    path.unlink(missing_ok=True)
                    with self.lock:
                        captures = list(self.captures.values())
                    for capture in captures:
                        if capture.directory is not None and not capture.finished:
                            try:
                                capture.stop("Recording service closed")
                            except (OSError, ValueError, TimeoutError):
                                logging.getLogger(__name__).exception("Could not finish service-owned capture")
                    self.workers.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", required=True)
    args = parser.parse_args()
    BrowserService(require_application=True).serve(args.serve)


if __name__ == "__main__":
    main()
