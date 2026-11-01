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
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from .call_lifecycle import BROWSER_RETURN_GRACE, CALL_END_GRACE, CONTROL_RECONNECT_GRACE, CallLifecycle

MAX_PACKET = 512 * 1024
# A call the user stopped stays stopped beyond the control link that carried
# the stop, so a reconnect does not start it again automatically.
STOPPED_CALL_GRACE = 10 * 60
STOPPED_CALL_LIMIT = 256
# A new extension worker reports the calls already open as its pages reconnect.
STOPPED_CALL_REPORT_WINDOW = 30


@dataclass(frozen=True)
class ControlReconnect:
    instance: str
    tab: int
    since: float
    lifecycle: bool


class StartRejected(ValueError):
    """The extension finished rejecting Start, rather than an unknown/late result."""


def service_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    parent = Path(runtime) if runtime else Path("/tmp") / f"cassini-{os.getuid()}"
    application = os.environ.get("FLATPAK_ID")
    if runtime and application in {"io.codemyriad.CassiniTalk", "io.codemyriad.CassiniTalk.Devel"}:
        # Shared by separate native-host/UI sandboxes of this deployment, but
        # distinct from native Cassini and the other Flatpak application ID.
        parent = parent / "app" / application
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
    command = [sys.executable, "-m", "cassini_talk.browser_service", "--serve", str(target)]
    if os.environ.get("FLATPAK_ID") in {"io.codemyriad.CassiniTalk", "io.codemyriad.CassiniTalk.Devel"}:
        from .flatpak_bridge import broker_command

        command = broker_command(target)
    subprocess.Popen(
        command,
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
    def __init__(self, stream, browser_name=None, browser_instance=None):
        self.browser_name = browser_name if isinstance(browser_name, str) and len(browser_name) <= 100 else None
        self.browser_instance = (
            browser_instance if isinstance(browser_instance, str) and 0 < len(browser_instance) <= 128 else None
        )
        self.id = str(uuid4())
        self.stream = stream
        self.write_lock = threading.RLock()
        self.meetings = {}
        self.status = {"recording": False}
        self.status_seen = self.hello_seen = False
        self.notifications = {}
        self.pending = {}
        self.commands = {}  # bounded correlation history for late results
        self.command_serial = 0
        self.stop_attempted = set()
        self.attempted = set()
        self.connected_at = time.monotonic()
        self.error = None
        self.busy = set()
        self.scheduled = set()
        self.missing_since = {}
        self.states = {}
        self.page_states = {}
        self.status_pages = {}
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
        self.call_captures = {}
        self.stopped_calls = {}
        self._capture_missing_since = {}
        self._capture_observed = set()
        self._capture_policy_pending = set()
        self._capture_lifecycles = {}
        self._control_reconnects = {}
        self._call_aliases = {}
        self._attempted_calls = set()
        self._scheduled_calls = set()
        self._call_lineage = {}
        self._call_versions = {}
        self._pending_calls = {}
        self._cancelled_captures = {}
        self._cancel_capture_pending = set()
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
        self.batch_timeout = 25
        self._stop_batches = set()
        self._stop_reasons = {}
        if notifier is None:
            from .browser_notifications import notify_recording

            notifier = notify_recording
        self.notifier = notifier
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cassini-browser")
        self.closed = threading.Event()

    def _owners(self):
        owners = {}
        for browser in sorted(self.browsers.values(), key=lambda browser: browser.connected_at):
            if not browser.closed:
                for tab in browser.meetings.keys() | browser.states.keys():
                    owners[(browser.browser_instance or browser.id, tab)] = browser
        return owners

    @staticmethod
    def _logical_id(identity):
        return "call/" + str(uuid5(NAMESPACE_URL, json.dumps(identity)))

    def _expected_call(self, identifier):
        saved = self._call_aliases.get(identifier)
        if saved is not None:
            return saved[0]
        owner, _, raw_tab = identifier.partition(":")
        browser = self.browsers.get(owner)
        if browser is not None and raw_tab.isdecimal():
            self._meeting_id(browser, int(raw_tab))
            saved = self._call_aliases.get(identifier)
            return saved[0] if saved is not None else None
        return None

    def _stopped_identity(self, identity):
        if identity is None:
            return False
        instance, tab, url = identity
        stopped = self.stopped_calls.get((instance, tab))
        return stopped is not None and stopped[0] == url

    def _clear_attempt(self, browser=None, tab=None, *, identity=None):
        """Forget transport and logical suppression together, for one call.

        A retiring worker can clear its own local flag, but cannot reset the
        replacement owner's attempt. Identity-only callers retire a known call
        after its stop expires or its native owner finishes.
        """
        if browser is not None:
            owner = self._owners().get((browser.browser_instance or browser.id, tab))
            if owner is not None and owner is not browser:
                browser.attempted.discard(tab)
                return
            browser.attempted.discard(tab)
            if identity is None and browser.browser_instance is not None:
                url = browser.meetings.get(tab, {}).get("url")
                if url is not None:
                    identity = (browser.browser_instance, tab, url)
        if identity is None:
            return
        instance, tab, url = identity
        self._attempted_calls.discard(self._logical_id(identity))
        for peer in self.browsers.values():
            if peer.browser_instance == instance and peer.meetings.get(tab, {}).get("url") == url:
                peer.attempted.discard(tab)

    def _cancel_capture(self, capture_id, identity=None, reason="user"):
        """Retire announced audio without blocking the inventory lock on a writer."""
        self._cancelled_captures[capture_id] = reason
        while len(self._cancelled_captures) > 1024:
            self._cancelled_captures.pop(next(iter(self._cancelled_captures)))
        if identity is not None:
            logical = self._logical_id(identity)
            for pending in self._pending_calls.get(logical, {}).values():
                if pending["action"] == "stop" and not pending.get("preserve_capture"):
                    pending.setdefault("captures", set()).add(capture_id)
        owner = self.captures.get(capture_id)
        if owner is None or owner.finished:
            return
        if identity is not None:
            owner.call_identity = identity
        owner.end_reason = owner.end_reason or reason
        owner.stop_requested.set()
        if capture_id not in self._cancel_capture_pending:
            self._cancel_capture_pending.add(capture_id)
            self.workers.submit(self._finish_cancelled_capture, capture_id, owner, reason)

    def _finish_cancelled_capture(self, capture_id, owner, reason):
        try:
            owner.stop(reason=reason)
        except (OSError, ValueError, TimeoutError):
            logging.getLogger(__name__).exception("Could not finish cancelled browser capture")
        finally:
            with self.lock:
                self._cancel_capture_pending.discard(capture_id)

    def _capture_stop_reason(self, browser, tab, identity):
        if self._stopped_identity(identity):
            return "user"
        if self._stopping_all.is_set():
            return next(reversed(self._stop_reasons.values()), "user")
        for command in reversed(tuple(browser.commands.values())):
            if command["tabId"] != tab:
                continue
            if (
                command["action"] == "stop"
                and not command.get("preserve_capture")
                and command.get("expected") == identity
            ):
                return command.get("reason", "user")
            break
        return None

    def _command_capture(self, metadata, capture, reason):
        with self.lock:
            owners = [self.captures[key] for key in metadata.get("captures", ()) if key in self.captures]
        if capture is not None and capture not in owners:
            owners.append(capture)
        for owner in owners:
            owner.stop(reason=reason)
        return capture or (owners[-1] if owners else None)

    @staticmethod
    def _result_tab(status, tab):
        if "sessions" in status:
            rows = status["sessions"]
            if not isinstance(rows, list):
                return None
            return next(
                (row for row in rows if isinstance(row, dict) and row.get("tabId") == tab),
                dict(recording=False, tabId=tab, path=status.get("path")),
            )
        if status.get("tabId") in (None, tab):
            return dict(status, tabId=tab)
        return None

    def _command_result(self, browser, packet):
        request_id = packet.get("requestId")
        pending = browser.pending.get(request_id)
        metadata = pending or browser.commands.get(request_id)
        if metadata is None:
            return
        status = packet.get("status")
        if isinstance(status, dict):
            tab = metadata["tabId"]
            target = self._result_tab(status, tab)
            newer = any(
                command["tabId"] == tab and command.get("serial", 0) > metadata.get("serial", 0)
                for command in browser.commands.values()
            )
            identity = metadata.get("expected")
            cancelled = metadata["action"] == "start" and (
                metadata.get("cancelled")
                or metadata.get("version") != self._call_versions.get(metadata.get("logical"), 0)
                or self._stopped_identity(identity)
            )
            if newer or cancelled:
                if (
                    metadata["action"] == "start"
                    and target is not None
                    and target.get("recording")
                    and isinstance(target.get("captureId"), str)
                ):
                    self._cancel_capture(target["captureId"], identity)
            elif pending is not None:
                self._status(browser, status)
            elif target is not None:
                # A retired response is authoritative for its own tab only.
                # Keep newer facts for the other independently managed calls.
                preserved = []
                for other_tab, state in browser.states.items():
                    if other_tab == tab:
                        continue
                    state = dict(state)
                    state.pop("pageState", None)
                    if other_tab in browser.page_states:
                        state["pageState"] = browser.page_states[other_tab]
                    preserved.append(state)
                self._status(browser, dict(status, sessions=[*preserved, target]))
        if pending is not None:
            pending["result"] = packet
            pending["event"].set()

    def _cancel_call(self, logical):
        self._call_versions[logical] = self._call_versions.get(logical, 0) + 1
        if len(self._call_versions) > 1024:
            protected = self._scheduled_calls | self._pending_calls.keys()
            for old in tuple(self._call_versions):
                if len(self._call_versions) <= 1024:
                    break
                if (
                    old not in protected
                    and old not in self._call_aliases
                    and old.partition(":")[0] not in self.browsers
                ):
                    self._call_versions.pop(old, None)
        for pending in self._pending_calls.get(logical, {}).values():
            if pending["action"] == "start":
                pending["cancelled"] = True
                pending["event"].set()

    def _validate_call(self, browser, tab, expected, *, version=None):
        if (
            expected is not None
            and (browser.browser_instance, tab, browser.meetings.get(tab, {}).get("url")) != expected
        ):
            raise ValueError("This meeting is no longer available")
        logical = self._logical_id(expected) if expected is not None else f"{browser.id}:{tab}"
        if version is not None and (
            version != self._call_versions.get(logical, 0)
            or self._stopped_identity(expected)
            or browser.states.get(tab, {}).get("recording")
        ):
            raise ValueError("Recording start was cancelled")
        return logical

    def _bound_capture(self, expected):
        if expected is None:
            return None
        instance, tab, url = expected
        binding = self.call_captures.get((instance, tab))
        if binding is not None and binding[1] == url:
            return self.captures.get(binding[0])
        return next(
            (
                capture
                for capture in self.captures.values()
                if getattr(capture, "call_identity", None) == expected and not capture.finished
            ),
            None,
        )

    def _meeting_id(self, browser, tab):
        meeting = browser.meetings.get(tab)
        binding = self.call_captures.get((browser.browser_instance, tab))
        if (
            binding is not None
            and binding[1] is not None
            and browser.states.get(tab, {}).get("captureId") == binding[0]
        ):
            meeting = dict(url=binding[1])
        if browser.browser_instance is None or meeting is None:
            return f"{browser.id}:{tab}"
        identity = (browser.browser_instance, tab, meeting["url"])
        identifier = self._logical_id(identity)
        entry = (identity, time.monotonic())
        self._call_aliases[identifier] = entry
        self._call_aliases[f"{browser.id}:{tab}"] = entry
        # Bound disconnected history, retaining active and queued identities.
        if len(self._call_aliases) <= 1024:
            return identifier
        protected = (
            self._scheduled_calls
            | self._pending_calls.keys()
            | {
                key
                for key, (value, _) in self._call_aliases.items()
                if any(
                    b.browser_instance == value[0] and b.meetings.get(value[1], {}).get("url") == value[2]
                    for b in self.browsers.values()
                )
            }
        )
        for key in list(self._call_aliases):
            if len(self._call_aliases) <= 1024:
                break
            if key not in protected:
                self._call_aliases.pop(key)
                self._attempted_calls.discard(key)
                self._call_versions.pop(key, None)
        return identifier

    def _resolve_call(self, identifier, *, wait=False, deadline=None, for_stop=False):
        deadline = (
            min(deadline, time.monotonic() + CONTROL_RECONNECT_GRACE)
            if deadline is not None
            else time.monotonic() + CONTROL_RECONNECT_GRACE
        )
        while True:
            with self.lock:
                owner, _, raw_tab = identifier.partition(":")
                if (
                    for_stop
                    and owner in self.browsers
                    and raw_tab.isdecimal()
                    and int(raw_tab) in self.browsers[owner].states
                ):
                    return identifier
                saved = self._call_aliases.get(identifier)
                if saved is None:
                    if owner in self.browsers and raw_tab.isdecimal():
                        return identifier
                    raise ValueError("This browser is no longer connected")
                identity, _seen = saved
                instance, tab, url = identity
                candidates = []
                for browser in self.browsers.values():
                    if browser.closed or not browser.status_seen or browser.browser_instance != instance:
                        continue
                    state = browser.states.get(tab, {})
                    capture = self.captures.get(state.get("captureId"))
                    binding = self.call_captures.get((instance, tab))
                    bound = (
                        binding == (state.get("captureId"), url)
                        or capture is not None
                        and getattr(capture, "call_identity", None) == identity
                    )
                    if browser.meetings.get(tab, {}).get("url") == url or for_stop and bound:
                        candidates.append(browser)
                if candidates:
                    browser = max(candidates, key=lambda browser: browser.connected_at)
                    if for_stop:
                        return f"{browser.id}:{tab}"
                    lineage = self._call_lineage.get((instance, tab))
                    if lineage is not None and lineage[0] != url:
                        raise ValueError("This meeting is no longer available")
                    return f"{browser.id}:{tab}"
                lineage = self._call_lineage.get((instance, tab))
                if not for_stop and lineage is not None and lineage[0] != url:
                    raise ValueError("This meeting is no longer available")
            if not wait or time.monotonic() >= deadline or self.closed.wait(0.02):
                raise ValueError("This browser is no longer connected")

    def _recover_command(self, browser, tab, action, pending, deadline):
        if browser.browser_instance is None:
            return None
        with self.lock:
            identifier = self._meeting_id(browser, tab)
        deadline = min(deadline, time.monotonic() + CONTROL_RECONNECT_GRACE)
        while not self.closed.is_set() and time.monotonic() < deadline:
            with self.lock:
                if pending["action"] == "start" and (
                    pending.get("cancelled")
                    or pending.get("generation") != self._stop_generation
                    or pending.get("version") != self._call_versions.get(pending["logical"], 0)
                    or self._stopped_identity(self._expected_call(pending["logical"]))
                ):
                    return None
                try:
                    current = self._resolve_call(identifier)
                except ValueError:
                    current = None
                if current is not None:
                    owner, _, raw_tab = current.partition(":")
                    replacement = self.browsers.get(owner)
                    if replacement is not None and replacement is not browser:
                        state = replacement.states.get(int(raw_tab), {})
                        if bool(state.get("recording")) == (action == "start"):
                            return dict(ok=True, status=dict(state))
            self.closed.wait(0.02)
        return None

    def _remember_meeting_ids(self, browser):
        owners = self._owners()
        for tab, meeting in browser.meetings.items():
            identifier = self._meeting_id(browser, tab)
            if (
                tab in browser.attempted
                and browser.browser_instance is not None
                and owners.get((browser.browser_instance, tab)) is browser
            ):
                self._attempted_calls.add(identifier)
            if browser.browser_instance is not None:
                key = (browser.browser_instance, tab)
                previous = self._call_lineage.get(key)
                if previous is None or previous[1] <= browser.connected_at:
                    self._call_lineage[key] = (meeting["url"], browser.connected_at)
        while len(self._call_lineage) > 512:
            oldest = min(self._call_lineage, key=lambda key: self._call_lineage[key][1])
            self._call_lineage.pop(oldest)

    def snapshot(self):
        # Never hold the inventory lock while waiting for a writer lock: PCM
        # and finalization report changes in the opposite direction.
        with self.lock:
            captures = list(self.captures.values())
        owned = {c.capture_id: c.snapshot() for c in captures if c.directory is not None}
        with self.lock:
            meetings, statuses, seen = [], [], set()
            owners = self._owners()
            for browser in self.browsers.values():
                for tab, value in browser.states.items():
                    if owners[(browser.browser_instance or browser.id, tab)] is not browser:
                        continue
                    value = dict(value)
                    capture = owned.get(value.get("captureId"))
                    if capture is not None:
                        value.update(capture)
                        seen.add(capture["captureId"])
                    value["id"] = self._meeting_id(browser, tab)
                    statuses.append(value)
                for meeting in browser.meetings.values():
                    if owners[(browser.browser_instance or browser.id, meeting["tabId"])] is not browser:
                        continue
                    value = next((v for v in statuses if v["id"] == self._meeting_id(browser, meeting["tabId"])), {})
                    meetings.append(
                        dict(
                            meeting,
                            id=self._meeting_id(browser, meeting["tabId"]),
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
            with self.lock:
                self._control_reconnects.pop(capture.capture_id, None)
                for (instance, tab), (capture_id, url) in self.call_captures.items():
                    if capture_id == capture.capture_id:
                        capture.call_identity = (instance, tab, url)
                        if getattr(capture, "user_stopped", False):
                            self._store_stop((instance, tab), url)
                            if url is not None:
                                self._cancel_call(self._logical_id((instance, tab, url)))
                        elif url is not None and not self._stopped_identity((instance, tab, url)):
                            self._clear_attempt(identity=(instance, tab, url))
                self.call_captures = {
                    key: value for key, value in self.call_captures.items() if value[0] != capture.capture_id
                }
                for browser in self.browsers.values():
                    for state in browser.states.values():
                        if state.get("captureId") == capture.capture_id:
                            state["recording"] = False
                finished = [key for key, owner in self.captures.items() if owner.finished]
                for key in finished[:-19]:
                    self.captures.pop(key, None)
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
                            if identifier in self._cancelled_captures or self._stopping_all.is_set():
                                raise ProtocolError("state", "Recording start was cancelled")
                            if any(
                                capture_id == identifier and self.stopped_calls.get(key, (None,))[0] == url
                                for key, (capture_id, url) in self.call_captures.items()
                                if url is not None
                            ):
                                raise ProtocolError("state", "Recording start was cancelled")
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
                        capture.stop(problem, reason="error" if problem else "appQuit")
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
                self.stopped_calls.clear()
                self._attempted_calls.clear()
                for browser in self.browsers.values():
                    browser.attempted.clear()
        self.policy()
        return self.snapshot()

    def _status(self, browser, value):
        if type(value.get("recording")) is not bool:
            return
        browser.status_seen = True
        previous = browser.states
        received_pages = {}
        browser.status = {
            key: value[key]
            for key in ("recording", "tabId", "path", "message", "tracks", "stopping", "captureId", "pageState")
            if key in value
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
                if item.get("pageState") in ("present", "navigating", "closed"):
                    received_pages[tab] = item["pageState"]
                if tab in states or not 0 <= tab <= 9007199254740991:
                    raise ValueError("Invalid or duplicate recording tab")
                states[tab] = {
                    key: item[key]
                    for key in ("recording", "tabId", "path", "message", "tracks", "stopping", "captureId", "pageState")
                    if key in item
                }
        else:
            tab = value.get("tabId")
            if type(tab) is int and value.get("pageState") in ("present", "navigating", "closed"):
                received_pages[tab] = value["pageState"]
            if type(tab) is int and "independent-sessions-v1" in browser.capabilities:
                # An independent tab status is a delta, not a full inventory.
                states = dict(previous)
                if value["recording"]:
                    states[tab] = dict(browser.status)
                else:
                    states.pop(tab, None)
            else:
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
            state = states.get(tab, {})
            capture_id = old.get("captureId")
            if (
                old.get("recording")
                and isinstance(capture_id, str)
                and capture_id
                and (owner is None or not owner.finished)
                and state.get("recording")
                and state.get("captureId", capture_id) == capture_id
            ):
                # Keep ownership through a page-state delta, including before
                # the audio socket opens. Explicit changes still take priority.
                for key in ("captureId", "path"):
                    if key in old:
                        state.setdefault(key, old[key])
                if "sessions" not in value and "stopping" in old:
                    state.setdefault("stopping", old["stopping"])
        if browser.browser_instance is not None:
            for (instance, tab), (capture_id, _url) in list(self.call_captures.items()):
                owner = self.captures.get(capture_id)
                if (
                    instance == browser.browser_instance
                    and tab in (browser.meetings.keys() | states.keys() | previous.keys())
                    and owner is not None
                    and not owner.finished
                ):
                    state = states.setdefault(
                        tab, dict(recording=True, tabId=tab, captureId=capture_id, path=str(owner.directory))
                    )
                    if state.get("recording") and not state.get("captureId"):
                        # A tab-scoped compatibility status can omit captureId.
                        # Preserve the independently owned audio association.
                        state.update(captureId=capture_id, path=str(owner.directory))
                    browser.attempted.add(tab)
            for tab, state in states.items():
                capture_id = state.get("captureId")
                owner = self.captures.get(capture_id)
                if (
                    isinstance(capture_id, str)
                    and capture_id
                    and state.get("recording")
                    and (owner is None or not owner.finished)
                ):
                    # The extension announces the identity before opening its
                    # audio socket. Preserve that association across an early
                    # control disconnect, including the original call address.
                    self._bind_capture(browser, tab, capture_id)
        for tab, state in tuple(states.items()):
            if tab not in received_pages and "pageState" in state:
                states[tab] = {key: value for key, value in state.items() if key != "pageState"}
        for tab, page in received_pages.items():
            if page in ("navigating", "closed") and tab in browser.meetings:
                # A fast returning status can precede its asynchronous inventory.
                # The departing document's positive count cannot prove return.
                browser.meetings[tab] = dict(browser.meetings[tab], sourceCount=0, heard=False)
                if "multi-stream-v1" in browser.capabilities:
                    browser.meetings[tab]["sourceCountKnown"] = False
        browser.stop_attempted = {
            (tab, capture_id)
            for tab, capture_id in browser.stop_attempted
            if states.get(tab, {}).get("recording") and states[tab].get("captureId") == capture_id
        }
        browser.status_pages = received_pages
        browser.states = states
        browser.page_states.update(received_pages)
        for tab in tuple(browser.page_states):
            if tab not in received_pages and tab not in browser.meetings and browser.page_states[tab] != "closed":
                browser.page_states.pop(tab, None)
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
                    if value.get("pageState") in ("present", "navigating", "closed"):
                        meetings[value["tabId"]]["pageState"] = value["pageState"]
                        browser.page_states[value["tabId"]] = value["pageState"]
                    meetings[value["tabId"]]["heard"] = value.get("heard") is True
                    meetings[value["tabId"]]["sourceCountKnown"] = value.get("sourceCountKnown", True) is True
                    meetings[value["tabId"]]["title"] = value["title"][:200]
                    meetings[value["tabId"]]["url"] = value["url"][:2048]
                self._expire_stopped(time.monotonic())
                self._remember_meeting_ids(browser)
                for tab in browser.meetings.keys() - meetings.keys():
                    identifier = self._meeting_id(browser, tab)
                    owner = self.captures.get(browser.states.get(tab, {}).get("captureId"))
                    if identifier not in self._scheduled_calls and (owner is None or owner.finished):
                        self._clear_attempt(browser, tab)
                    if browser.browser_instance is not None and (owner is None or owner.finished):
                        key = (browser.browser_instance, tab)
                        lineage = self._call_lineage.get(key)
                        if lineage is None or browser.connected_at >= lineage[1]:
                            self._call_lineage[key] = (None, browser.connected_at)
                for tab in browser.meetings.keys() & meetings.keys():
                    if browser.meetings[tab]["url"] != meetings[tab]["url"]:
                        self._clear_attempt(browser, tab)
                previous, browser.meetings = browser.meetings, meetings
                for tab in previous.keys() - meetings.keys():
                    if tab not in browser.status_pages and browser.page_states.get(tab) != "closed":
                        browser.page_states.pop(tab, None)
                        if tab in browser.states:
                            browser.states[tab] = {
                                key: value for key, value in browser.states[tab].items() if key != "pageState"
                            }
                self._restore_stopped(browser, previous)
                for tab in meetings:
                    if self._meeting_id(browser, tab) in self._attempted_calls:
                        browser.attempted.add(tab)
                self._remember_meeting_ids(browser)
                for tab, state in browser.states.items():
                    if state.get("recording") and isinstance(state.get("captureId"), str):
                        self._bind_capture(browser, tab, state["captureId"])
                if browser.browser_instance is not None:
                    for (instance, tab), (capture_id, _url) in self.call_captures.items():
                        owner = self.captures.get(capture_id)
                        if (
                            instance == browser.browser_instance
                            and tab in meetings
                            and owner is not None
                            and not owner.finished
                        ):
                            browser.states.setdefault(
                                tab, dict(recording=True, tabId=tab, captureId=capture_id, path=str(owner.directory))
                            )
                            browser.attempted.add(tab)
                browser.attempted.intersection_update(meetings)
            elif kind == "status":
                self._status(browser, packet)
            elif kind == "result":
                self._command_result(browser, packet)
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
            browser.page_states = {
                tab: page
                for tab, page in browser.page_states.items()
                if tab in browser.states or tab in browser.meetings
            }
        self.policy()

    def command(
        self,
        identifier,
        action,
        *,
        microphone=True,
        preserve_capture=False,
        user=False,
        reason="user",
        deadline=None,
        cancelled=None,
        call_version=None,
        expected_capture=None,
    ):
        from .session_events import END_REASONS

        deadline = (
            min(deadline, time.monotonic() + self.command_timeout)
            if deadline is not None
            else time.monotonic() + self.command_timeout
        )
        if action == "start" and (time.monotonic() >= deadline or cancelled is not None and cancelled.is_set()):
            raise ValueError("Recording start was cancelled")

        if (
            action not in ("start", "stop")
            or not isinstance(identifier, str)
            or type(microphone) is not bool
            or not isinstance(reason, str)
            or reason not in END_REASONS
        ):
            raise ValueError("Invalid meeting command")
        if identifier.startswith("capture:"):
            with self.lock:
                capture = self.captures.get(identifier.removeprefix("capture:"))
            if action != "stop" or capture is None:
                raise ValueError("This meeting is no longer available")
            with self.lock:
                identity = getattr(capture, "call_identity", None)
                if identity is None:
                    identity = next(
                        (
                            (instance, tab, url)
                            for (instance, tab), (capture_id, url) in self.call_captures.items()
                            if capture_id == capture.capture_id and url is not None
                        ),
                        None,
                    )
                if identity is not None:
                    self._cancel_call(self._logical_id(identity))
            if user:
                self._remember_capture_stop(capture)
            capture.stop(reason=reason)
            return self.snapshot()
        with self.lock:
            expected = self._expected_call(identifier)
            selected = self.captures.get(expected_capture) if expected_capture is not None else None
            if selected is not None and getattr(selected, "call_identity", None) is not None:
                expected = selected.call_identity
            logical = self._logical_id(expected) if expected is not None else identifier
            local = (
                (selected if expected_capture is not None else self._bound_capture(expected))
                if action == "stop" and not preserve_capture
                else None
            )
            recovering = bool(self._pending_calls.get(logical))
            if action == "stop" and not preserve_capture:
                self._cancel_call(logical)
                if user and expected is not None:
                    lineage = self._call_lineage.get((expected[0], expected[1]))
                    if (
                        local is not None
                        or recovering
                        or logical in self._scheduled_calls
                        or lineage is None
                        or lineage[0] == expected[2]
                    ):
                        self._store_stop((expected[0], expected[1]), expected[2])
        if local is not None:
            local.stop(reason=reason)
        try:
            identifier = self._resolve_call(
                identifier, wait=action == "start", deadline=deadline, for_stop=action == "stop"
            )
        except ValueError:
            if local is not None and local.finished or action == "stop" and recovering:
                return dict(self.snapshot(), stoppedPath=str(local.directory) if local is not None else None)
            raise
        browser_id, _, raw_tab = identifier.partition(":")
        with self.lock:
            browser = self.browsers.get(browser_id)
            if browser is None or not raw_tab.isdecimal():
                raise ValueError("This browser is no longer connected")
            tab = int(raw_tab)
            if action == "start" and (
                self._stopping_all.is_set()
                or time.monotonic() >= deadline
                or cancelled is not None
                and cancelled.is_set()
            ):
                raise ValueError("Recording start was cancelled")
            independent = "independent-sessions-v1" in browser.capabilities
            if action == "start":
                if tab not in browser.meetings:
                    raise ValueError("This meeting is no longer available")
                self._validate_call(browser, tab, expected, version=call_version)
            if expected_capture is not None and browser.states.get(tab, {}).get("captureId") != expected_capture:
                raise ValueError("This meeting is no longer available")
            capture = self.captures.get(browser.states.get(tab, {}).get("captureId"))
            joining = (
                next((p for p in browser.pending.values() if p.get("action") == "stop" and p["tabId"] == tab), None)
                if action == "stop"
                else None
            )
            if action == "stop" and user and expected is None:
                self._remember_stop(browser, tab, True)
                if capture is not None and not preserve_capture:
                    capture.end_reason = capture.end_reason or reason
                    capture.stop_requested.set()
            if action == "start" and any(
                p.get("action") == "start" for p in self._pending_calls.get(logical, {}).values()
            ):
                raise ValueError("A recording command is still being processed")
            pending_start = any(p.get("action") == "start" and p["tabId"] == tab for p in browser.pending.values())
            if action == "stop":
                for pending in browser.pending.values():
                    if pending.get("action") == "start" and pending["tabId"] == tab:
                        pending["cancelled"] = True
            if (
                joining is None
                and (tab in browser.busy or not independent and browser.busy)
                and not (action == "stop" and pending_start)
            ):
                raise ValueError("A recording command is still being processed")
            if joining is None:
                if (
                    action == "stop"
                    and not browser.states.get(tab, {}).get("recording")
                    and not independent
                    and not pending_start
                ):
                    raise ValueError("This meeting is not recording")
                if action == "start" and user:
                    self._cancel_call(logical)
                if action == "stop" and browser.states.get(tab, {}).get("recording"):
                    # This transport already received Stop for this capture.
                    # Only a newly reported capture or returning worker needs
                    # policy to issue another; stale state after a timeout must
                    # not race the user's next manual Record with a retry.
                    browser.stop_attempted.add((tab, browser.states[tab].get("captureId")))
                browser.busy.add(tab)
                browser.error = None
                if user and (action == "start" or expected is None):
                    self._remember_stop(browser, tab, action == "stop")
                browser.attempted.add(tab)
                if browser.browser_instance is not None:
                    self._attempted_calls.add(self._meeting_id(browser, tab))
                request_id = str(uuid4())
                browser.command_serial += 1
                pending = dict(
                    serial=browser.command_serial,
                    expected=expected,
                    reason=reason,
                    preserve_capture=preserve_capture,
                    captures=set(),
                    event=threading.Event(),
                    result=None,
                    action=action,
                    tabId=tab,
                    logical=logical,
                    version=self._call_versions.get(logical, 0),
                    generation=self._stop_generation,
                )
                browser.pending[request_id] = pending
                browser.commands[request_id] = pending
                for old_id in tuple(browser.commands):
                    if len(browser.commands) <= 256:
                        break
                    if old_id not in browser.pending:
                        browser.commands.pop(old_id)
                self._pending_calls.setdefault(logical, {})[request_id] = pending
                capture = self.captures.get(browser.states.get(tab, {}).get("captureId"))
                if capture is not None:
                    pending["captures"].add(capture.capture_id)
                if capture is not None and action == "stop" and not preserve_capture:
                    capture.end_reason = capture.end_reason or reason
                    capture.stop_requested.set()
        if joining is not None:
            if capture is not None and not preserve_capture:
                # Local save is authoritative. The existing browser Stop may
                # still be waiting for an unreachable page; do not keep the
                # user's Stop waiting after their audio is safely saved.
                capture.stop(reason=reason)
                return dict(self.snapshot(), stoppedPath=str(capture.directory))
            if not joining["event"].wait(min(self.command_timeout, max(0, deadline - time.monotonic()))):
                raise TimeoutError("The browser did not finish the recording command")
            if not preserve_capture:
                capture = self._command_capture(joining, capture, reason)
            result = joining["result"]
            if (result is None or result.get("ok") is not True) and (capture is None or not capture.finished):
                raise ValueError(str((result or {}).get("error", "The browser disconnected"))[:500])
            return dict(
                self.snapshot(),
                stoppedPath=str(capture.directory) if capture is not None else (result.get("status") or {}).get("path"),
            )
        try:
            if capture is not None and action == "stop" and not preserve_capture:
                capture.stop(reason=reason)
            packet = dict(type="command", requestId=request_id, action=action, tabId=tab, mode="tracks")
            if independent:
                packet.update(independent=True, captureLocal=False, microphone=microphone)
                if "page-state-v1" in browser.capabilities:
                    if action == "start":
                        packet["keepOnNavigation"] = True
                    else:
                        packet["reason"] = reason
            with browser.write_lock:
                if (
                    action == "start"
                    and expected is not None
                    and (browser.browser_instance, tab, browser.meetings.get(tab, {}).get("url")) != expected
                ):
                    raise ValueError("This meeting is no longer available")
                if action == "start" and (
                    pending.get("cancelled")
                    or cancelled is not None
                    and cancelled.is_set()
                    or call_version is not None
                    and call_version != self._call_versions.get(logical, 0)
                ):
                    raise ValueError("Recording start was cancelled")
                browser.send(packet)
            if capture is not None and capture.finished and action == "stop" and not preserve_capture:
                return dict(self.snapshot(), stoppedPath=str(capture.directory))
            if not pending["event"].wait(min(self.command_timeout, max(0, deadline - time.monotonic()))):
                raise TimeoutError("The browser did not finish the recording command")
            if action == "stop" and not preserve_capture:
                capture = self._command_capture(pending, capture, reason)
            result = pending["result"]
            if action == "start" and pending.get("cancelled"):
                raise ValueError("Recording start was cancelled")
            if result is None and browser.closed:
                result = self._recover_command(browser, tab, action, pending, deadline)
            if result is None or result.get("ok") is not True:
                error = (
                    StartRejected
                    if result is not None and action == "start" and not pending.get("cancelled")
                    else ValueError
                )
                raise error(str((result or {}).get("error", "The browser disconnected"))[:500])
            snapshot = self.snapshot()
            if action == "stop":
                snapshot["stoppedPath"] = (
                    str(capture.directory)
                    if capture is not None and capture.directory is not None
                    else (result.get("status") or {}).get("path")
                )
            return snapshot
        except (OSError, ValueError, TimeoutError) as error:
            if capture is not None and capture.finished and action == "stop" and not preserve_capture:
                return dict(self.snapshot(), stoppedPath=str(capture.directory))
            with self.lock:
                browser.error = str(error)
            raise
        finally:
            with self.lock:
                # Also release joiners when send/timeout fails without a
                # result packet. They must not wait for another full timeout.
                pending["event"].set()
                browser.pending.pop(request_id, None)
                self._pending_calls.get(logical, {}).pop(request_id, None)
                if not self._pending_calls.get(logical):
                    self._pending_calls.pop(logical, None)
                if not any(p["tabId"] == tab for p in browser.pending.values()):
                    browser.busy.discard(tab)

    def _batch(self, jobs, deadline, *, cancelled=None):
        if not jobs:
            return [], []
        executor = ThreadPoolExecutor(max_workers=min(32, len(jobs)), thread_name_prefix="cassini-browser-batch")
        futures = [(identifier, executor.submit(job)) for identifier, job in jobs]
        remaining = {future for _identifier, future in futures}
        try:
            while remaining and time.monotonic() < deadline and not (cancelled is not None and cancelled.is_set()):
                _done, remaining = wait(
                    remaining, timeout=min(0.05, max(0, deadline - time.monotonic())), return_when=FIRST_COMPLETED
                )
            if remaining and cancelled is not None:
                cancelled.set()
                for future in remaining:
                    future.cancel()
            results = []
            for identifier, future in futures:
                if future.done() and not future.cancelled():
                    try:
                        results.append((identifier, future.result(), None))
                    except Exception as error:
                        if not isinstance(error, (OSError, ValueError, CancelledError)):
                            logging.getLogger(__name__).exception("Unexpected browser batch failure: %s", identifier)
                        results.append((identifier, None, error))
                else:
                    results.append((identifier, None, TimeoutError("The browser did not finish the recording command")))
            return results, [future for _identifier, future in futures]
        finally:
            executor.shutdown(wait=False, cancel_futures=cancelled is not None)

    def _release_stop_batch(self, token, futures):
        if any(not future.done() for future in futures):
            return
        with self.lock:
            if token not in self._stop_batches:
                return
            self._stop_batches.discard(token)
            self._stop_reasons.pop(token, None)
            if not self._stop_batches:
                self._stopping_all.clear()
        self.policy()

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
            owners = self._owners()
            targets = [
                (self._meeting_id(b, tab), dict(meeting))
                for b in self.browsers.values()
                if b.status_seen
                for tab, meeting in b.meetings.items()
                if owners.get((b.browser_instance or b.id, tab)) is b
                and meeting.get("sourceCount", 0) > 0
                and not b.states.get(tab, {}).get("recording")
            ]
            self._record_groups[token] = group
        deadline = time.monotonic() + self.batch_timeout

        def start_one(identifier, meeting):
            if group.is_set() or self._stopping_all.is_set() or time.monotonic() >= deadline:
                raise ValueError("Recording start was cancelled")
            cleanup_sent = False
            try:
                return self.command(
                    identifier, "start", microphone=microphone, user=True, deadline=deadline, cancelled=group
                )
            except StartRejected:
                if group.is_set() or self._stopping_all.is_set() or time.monotonic() >= deadline:
                    raise ValueError("Recording start was cancelled") from None
                return self.start_fallback(identifier, meeting, microphone, cancelled=group, deadline=deadline)
            except TimeoutError:
                # No acknowledgement means Start may still be finishing in the
                # browser. Retire that attempt before its caller can retry it.
                cleanup_sent = True
                with contextlib.suppress(OSError, ValueError, TimeoutError):
                    self.command(identifier, "stop", user=True)
                raise
            finally:
                if not cleanup_sent and group.is_set() and time.monotonic() >= deadline:
                    # The request can return while a late accepted Start is
                    # being retired. Never leave that unacknowledged capture on.
                    with contextlib.suppress(OSError, ValueError, TimeoutError):
                        self.command(identifier, "stop", user=True)

        try:
            jobs = [
                (identifier, lambda identifier=identifier, meeting=meeting: start_one(identifier, meeting))
                for identifier, meeting in targets
            ]
            results, _futures = self._batch(jobs, deadline, cancelled=group)
            titles = {identifier: meeting["title"] for identifier, meeting in targets}
            failures = [
                dict(id=identifier, title=titles[identifier], error=str(error))
                for identifier, _result, error in results
                if error is not None
            ]
            return dict(self.snapshot(), attempted=len(targets), cancelled=group.is_set(), failures=failures)
        finally:
            with self.lock:
                self._record_groups.pop(token, None)

    def start_fallback(
        self,
        identifier,
        meeting,
        microphone=True,
        *,
        cancelled=None,
        automatic_generation=None,
        deadline=None,
        call_version=None,
    ):
        from .browser_capture import BrowserCapture

        with self.lock:
            expected = self._expected_call(identifier)
        identifier = self._resolve_call(identifier, wait=True, deadline=deadline)
        browser_id, _, raw_tab = identifier.partition(":")
        with self.lock:
            browser = self.browsers.get(browser_id)
            if (
                browser is None
                or self._stopping_all.is_set()
                or (cancelled is not None and cancelled.is_set())
                or (deadline is not None and time.monotonic() >= deadline)
                or (
                    automatic_generation is not None
                    and (
                        automatic_generation != self._stop_generation
                        or not self.auto_record
                        or self.require_application
                        and not any(self.applications.values())
                        or "call-evidence-v1" in browser.capabilities
                        and not self._auto_start_ready(browser, browser.meetings.get(int(raw_tab), {}))
                    )
                )
            ):
                raise ValueError("Recording start was cancelled")
            tab = int(raw_tab)
            self._validate_call(browser, tab, expected, version=call_version)
            if meeting.get("url") != browser.meetings.get(tab, {}).get("url"):
                raise ValueError("This meeting is no longer available")
            existing = self.captures.get(browser.states.get(tab, {}).get("captureId"))
            if existing is None or existing.finished:
                if browser.states.get(tab, {}).get("recording"):
                    raise ValueError("A recording command is still being processed")
                capture_id = str(uuid4())
                capture = BrowserCapture(
                    self._capture_changed, parent=self.capture_parent, tap_factory=self.tap_factory
                )
                capture.browser_lost = True
                capture.browser_lost_since = time.monotonic()
                self.captures[capture_id] = capture
                browser.states[tab] = dict(recording=True, captureId=capture_id, tabId=tab)
                if browser.browser_instance is not None:
                    self._bind_capture(browser, tab, capture_id)
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
                if capture.directory is None:
                    self.captures.pop(capture_id, None)
                    self.call_captures = {
                        key: value for key, value in self.call_captures.items() if value[0] != capture_id
                    }
                if browser.states.get(tab, {}).get("captureId") == capture_id:
                    browser.states[tab] = dict(recording=False, tabId=tab)
            raise
        if cancelled is not None and cancelled.is_set() or deadline is not None and time.monotonic() >= deadline:
            capture.stop(reason="user")
            raise ValueError("Recording start was cancelled")
        return capture.snapshot()

    def stop_all(self, directory=None, *, cancel_groups=(), quitting_pid=None, quitting_generation=0, reason="user"):
        from .session_events import END_REASONS

        reason = "appQuit" if quitting_pid is not None else reason
        if not isinstance(reason, str) or reason not in END_REASONS:
            raise ValueError("Invalid recording end reason")
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

        stop_token = str(uuid4())
        futures = []
        deadline = time.monotonic() + self.batch_timeout
        if directory is None:
            with self.lock:
                self._stop_batches.add(stop_token)
                self._stop_reasons[stop_token] = reason
                self._stopping_all.set()
                self._stop_generation += 1
                for logical, pending_calls in tuple(self._pending_calls.items()):
                    if any(pending["action"] == "start" for pending in pending_calls.values()):
                        expected = self._expected_call(logical)
                        self._cancel_call(logical)
                        if quitting_pid is None and expected is not None:
                            self._store_stop((expected[0], expected[1]), expected[2])
                if quitting_pid is None:
                    # The selected call may have finished locally between the
                    # user's click and this RPC. Stop also cancels its retry.
                    for browser in self.browsers.values():
                        for tab, meeting in browser.meetings.items():
                            if meeting.get("sourceCount", 0) > 0 or browser.states.get(tab, {}).get("recording"):
                                self._remember_stop(browser, tab, True)
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
            paths = [
                s["path"]
                for s in snapshot["statuses"]
                if (s.get("recording") or s.get("stopping")) and matches(s.get("path"))
            ]
            with self.lock:
                commands = {s["id"] for s in snapshot["statuses"] if s.get("recording") and matches(s.get("path"))}
                if directory is None:
                    commands.update(
                        self._meeting_id(b, p["tabId"])
                        for b in self.browsers.values()
                        for p in b.pending.values()
                        if p.get("action") == "start"
                    )
            with self.lock:
                captures = [
                    (key, c)
                    for key, c in self.captures.items()
                    if not c.finished and (directory is None or c.directory is not None and matches(str(c.directory)))
                ]
                for _key, capture in captures:
                    if quitting_pid is None:
                        self._remember_capture_stop(capture)
                    capture.end_reason = capture.end_reason or reason
                    capture.stop_requested.set()
            # Save native-owned audio before waiting for any browser acknowledgement.
            jobs = [
                ("capture:" + key, lambda capture=capture: capture.stop(reason=reason)) for key, capture in captures
            ]
            jobs.extend(
                (
                    identifier,
                    lambda identifier=identifier: self.command(
                        identifier, "stop", user=quitting_pid is None, reason=reason, deadline=deadline
                    ),
                )
                for identifier in commands
            )
            results, futures = self._batch(jobs, deadline)
            errors = []
            current = self.snapshot()
            active = {state["id"] for state in current["statuses"] if state.get("recording")}
            for identifier, result, error in results:
                if result is not None and isinstance(result.get("stoppedPath", result.get("path")), str):
                    paths.append(result.get("stoppedPath", result.get("path")))
                if error is not None and (identifier.startswith("capture:") or identifier in active):
                    errors.append(error)
            if errors:
                raise TimeoutError(str(errors[0]))
            return dict(current, stoppedPaths=list(dict.fromkeys(paths)))
        finally:
            if directory is None:
                self._release_stop_batch(stop_token, futures)
                for future in futures:
                    future.add_done_callback(lambda _future: self._release_stop_batch(stop_token, futures))

    def policy(self):
        if self.closed.is_set() or self._stopping_all.is_set():
            return
        tasks = []
        capture_tasks = []
        with self.lock:
            generation = self._stop_generation
            now = time.monotonic()
            observed = {
                state.get("captureId"): (browser, tab, browser.meetings.get(tab))
                for browser in self.browsers.values()
                if not browser.closed and browser.status_seen
                for tab, state in browser.states.items()
                if state.get("recording")
            }
            for capture_id, reconnect in tuple(self._control_reconnects.items()):
                if capture_id not in self.captures and now - reconnect.since >= CONTROL_RECONNECT_GRACE:
                    self._control_reconnects.pop(capture_id, None)
            self._capture_observed.update(observed)
            self._capture_observed.intersection_update(self.captures)
            for key, capture in self.captures.items():
                if (
                    capture.finished
                    or capture.starting
                    or capture.stopping
                    or capture.stop_requested.is_set()
                    or key not in self._capture_observed
                    and key not in self._control_reconnects
                    and not capture.browser_lost
                ):
                    self._capture_missing_since.pop(key, None)
                    continue
                peer = observed.get(key)
                reconnect = self._control_reconnects.get(key)
                if reconnect is not None:
                    binding = self.call_captures.get((reconnect.instance, reconnect.tab))
                    if binding is None or binding[0] != key:
                        self._control_reconnects.pop(key, None)
                        reconnect = None
                    elif reconnect.lifecycle:
                        capture.lifecycle_capable = True
                if peer is not None and "page-state-v1" in peer[0].capabilities:
                    capture.lifecycle_capable = True
                if peer is not None and "multi-stream-v1" in peer[0].capabilities:
                    capture.multistream_capable = True
                if capture.lifecycle_capable:
                    lifecycle = self._capture_lifecycles.setdefault(key, CallLifecycle())
                    if key not in self._capture_observed and capture.browser_lost and lifecycle.missing is None:
                        lifecycle.missing = capture.browser_lost_since
                    page, count, meeting = None, 0, {}
                    if peer is not None:
                        state = peer[0].states[peer[1]]
                        meeting = peer[2] or {}
                        page = browser_page = peer[0].page_states.get(peer[1])
                        if browser_page is None:
                            page = state.get("pageState") or meeting.get("pageState")
                        count = meeting.get("sourceCount", 0)
                        if (
                            lifecycle.missing is not None
                            and "call-evidence-v1" in peer[0].capabilities
                            and meeting.get("heard") is not True
                            and not capture.browser_ready_since(lifecycle.missing)
                        ):
                            count = 0  # Returning pre-join slots are not received audio.
                    if page == "closed":
                        capture.tab_closed = True
                    if reconnect is not None:
                        known_page = peer is not None and peer[2] is not None
                        # A disconnect can be observed between two PCM buffers. Recent
                        # pre-disconnect PCM is still fresh; requiring the next
                        # buffer here prematurely invents a page-loss timer.
                        fresh = capture.browser_ready_since()
                        if known_page and page == "present":
                            self._control_reconnects.pop(key, None)
                            if lifecycle.missing == reconnect.since:
                                lifecycle.missing = None
                        elif page in ("closed", "navigating"):
                            self._control_reconnects.pop(key, None)
                            if not capture.browser_lost and fresh and lifecycle.missing == reconnect.since:
                                lifecycle.missing = None
                        elif (
                            not known_page
                            and now - reconnect.since < CONTROL_RECONNECT_GRACE
                            and not capture.tab_closed
                            and not capture.browser_lost
                            and fresh
                        ):
                            continue  # Rediscovery is not evidence that the page navigated.
                        elif lifecycle.missing is None and lifecycle.ended is None:
                            lifecycle.missing = reconnect.since
                    action = lifecycle.decide(
                        now,
                        page,
                        count,
                        fallback=capture.browser_lost,
                        others=capture.others,
                        restore_ready=capture.browser_ready,
                        multistream=capture.multistream_capable,
                        heard=meeting.get("heard") is True,
                        inventory_known=peer is not None
                        and peer[2] is not None
                        and meeting.get("sourceCountKnown", True),
                        control_alive=peer is not None,
                        pcm_live=capture.multistream_capable and capture.browser_ready_since(recorded=True),
                    )
                    if reconnect is not None and page == "present" and count > 0 and lifecycle.missing is None:
                        self._control_reconnects.pop(key, None)
                    identifier = f"{peer[0].id}:{peer[1]}" if peer is not None else None
                    if action and key not in self._capture_policy_pending:
                        self._capture_policy_pending.add(key)
                        capture_tasks.append((key, capture, identifier, action))
                    continue
                if peer is not None and peer[2] is not None and peer[2]["sourceCount"] > 0:
                    self._control_reconnects.pop(key, None)
                    self._capture_missing_since.pop(key, None)
                    continue
                if reconnect is not None:
                    self._capture_missing_since.setdefault(key, reconnect.since)
                    if (
                        now - reconnect.since < CONTROL_RECONNECT_GRACE
                        and not capture.browser_lost
                        and capture.browser_ready_since(reconnect.since)
                    ):
                        continue
                # A lost audio/control peer can disappear before its first
                # status was observed. Its native fallback still needs a
                # bounded lifetime, measured from loss, not a later policy tick.
                initial = now
                if key not in self._capture_observed and capture.browser_lost:
                    initial = capture.browser_lost_since if capture.browser_lost_since is not None else now
                since = self._capture_missing_since.setdefault(key, initial)
                # A reachable, still-recording page with zero sources has hung
                # up. An absent page/companion gets a bounded reconnection grace
                # while Q4 keeps audio. Never switch a fallback back to browser.
                ended = (
                    peer is not None
                    and peer[2] is not None
                    and not capture.browser_lost
                    and peer[0].states[peer[1]].get("stopping") is not True
                )
                identifier = f"{peer[0].id}:{peer[1]}" if peer is not None else None
                action = None
                if now - since >= (CALL_END_GRACE if ended else BROWSER_RETURN_GRACE):
                    action = "stop:callEnded" if ended else "stop:pageGone"
                elif not ended and not capture.browser_lost and (peer is not None or now - since >= CALL_END_GRACE):
                    action = "fallback"
                if action and key not in self._capture_policy_pending:
                    self._capture_policy_pending.add(key)
                    capture_tasks.append((key, capture, identifier, action))
            for key in self._capture_missing_since.keys() - self.captures.keys():
                self._capture_missing_since.pop(key, None)
            for key in self._capture_lifecycles.keys() - self.captures.keys():
                self._capture_lifecycles.pop(key, None)
            owners = self._owners()
            for browser in self.browsers.values():
                if browser.closed or not browser.status_seen:
                    continue
                independent = "independent-sessions-v1" in browser.capabilities
                for tab, state in browser.states.items():
                    if not state.get("recording") or tab in browser.busy or tab in browser.scheduled:
                        continue
                    expected = self._expected_call(self._meeting_id(browser, tab))
                    stopped_key = (tab, state.get("captureId"))
                    if owners.get((browser.browser_instance or browser.id, tab)) is browser and self._stopped_identity(
                        expected
                    ):
                        if stopped_key not in browser.stop_attempted:
                            browser.stop_attempted.add(stopped_key)
                            browser.scheduled.add(tab)
                            tasks.append((self._meeting_id(browser, tab), "stop", None))
                        continue
                    if browser.meetings.get(tab, {}).get("sourceCount", 0) > 0:
                        browser.missing_since.pop(tab, None)
                    elif state.get("captureId") in self.captures:
                        continue  # Independently owned sessions are handled above.
                    elif tab not in browser.missing_since:
                        browser.missing_since[tab] = time.monotonic()
                    elif time.monotonic() - browser.missing_since[tab] >= CALL_END_GRACE:
                        browser.scheduled.add(tab)
                        tasks.append((f"{browser.id}:{tab}", "stop", None))
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
                            owners.get((browser.browser_instance or browser.id, tab)) is browser
                            and tab not in browser.attempted
                            and self._meeting_id(browser, tab) not in self._scheduled_calls
                            and self._auto_start_ready(browser, meeting)
                            and not browser.states.get(tab, {}).get("recording")
                        ):
                            browser.attempted.add(tab)
                            browser.scheduled.add(tab)
                            identifier = self._meeting_id(browser, tab)
                            self._scheduled_calls.add(identifier)
                            if browser.browser_instance is not None:
                                self._attempted_calls.add(identifier)
                            tasks.append((identifier, "start", self._call_versions.get(identifier, 0)))
                            if not independent:
                                break
        for identifier, action, version in tasks:
            self.workers.submit(self._automatic, identifier, action, generation, version)
        for key, capture, identifier, action in capture_tasks:
            self.workers.submit(self._capture_lifecycle, key, capture, identifier, action)

    def _capture_lifecycle(self, key, capture, identifier, action):
        try:
            if action == "fallback":
                if identifier is not None:
                    self._fallback(identifier, capture)
                else:
                    capture.fallback()
            elif action == "restore":
                capture.restore()
            else:
                # Save before waiting for a potentially unreachable extension.
                reason = action.partition(":")[2] or "user"
                capture.stop(reason=reason)
                if identifier is not None:
                    self.command(identifier, "stop", reason=reason, expected_capture=key)
        except (OSError, ValueError, TimeoutError):
            logging.getLogger(__name__).exception("Browser capture lifecycle: %s", action)
        finally:
            with self.lock:
                self._capture_policy_pending.discard(key)

    def _fallback(self, identifier, capture):
        try:
            capture.fallback()
            if not capture.lifecycle_capable:
                self.command(identifier, "stop", preserve_capture=True)
        except (OSError, ValueError, TimeoutError) as error:
            logging.getLogger(__name__).warning("Browser source fallback: %s", error)
        finally:
            with self.lock:
                browser_id, _, tab = identifier.partition(":")
                if browser_id in self.browsers:
                    self.browsers[browser_id].scheduled.discard(int(tab))

    @staticmethod
    def _auto_start_ready(browser, meeting):
        return (
            meeting.get("sourceCountKnown", True) is True
            and meeting.get("sourceCount", 0) > 0
            and ("call-evidence-v1" not in browser.capabilities or meeting.get("heard") is True)
        )

    def _automatic(self, identifier, action, generation=None, call_version=None):
        logical_identifier = identifier
        with self.lock:
            expected = self._expected_call(identifier)
            if call_version is None:
                call_version = self._call_versions.get(identifier, 0)
        try:
            identifier = self._resolve_call(identifier, wait=True)
            if action == "start":
                with self.lock:
                    if (
                        self._stopping_all.is_set()
                        or not self.auto_record
                        or (generation is not None and generation != self._stop_generation)
                        or self.require_application
                        and not any(self.applications.values())
                    ):
                        return
                    browser_id, _, raw_tab = identifier.partition(":")
                    browser = self.browsers.get(browser_id)
                    if browser is None or (
                        "call-evidence-v1" in browser.capabilities
                        and not self._auto_start_ready(browser, browser.meetings.get(int(raw_tab), {}))
                    ):
                        # Evidence may disappear while this task waits for a worker.
                        if browser is not None:
                            self._clear_attempt(browser, int(raw_tab))
                        return
            if action == "start":
                with self.lock:
                    owner, _, raw_tab = identifier.partition(":")
                    browser = self.browsers.get(owner)
                    if browser is None:
                        return
                    self._validate_call(browser, int(raw_tab), expected, version=call_version)
                self.command(logical_identifier, action, call_version=call_version)
            else:
                self.command(logical_identifier, action)
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
            try:
                identifier = self._resolve_call(logical_identifier, wait=True)
            except ValueError:
                return
            browser_id, _, tab = identifier.partition(":")
            with self.lock:
                browser = self.browsers.get(browser_id)
                meeting = browser.meetings.get(int(tab)) if browser else None
                active = not self.require_application or any(self.applications.values())
                evidence = (
                    browser is None
                    or "call-evidence-v1" not in browser.capabilities
                    or self._auto_start_ready(browser, meeting or {})
                )
                allowed = self.auto_record and active and not self._stopping_all.is_set() and evidence
                if action == "start" and browser is not None and not evidence:
                    self._clear_attempt(browser, int(tab))
            if action == "start" and allowed and meeting is not None:
                try:
                    self.start_fallback(
                        logical_identifier, meeting, automatic_generation=generation, call_version=call_version
                    )
                except (OSError, ValueError, TimeoutError):
                    logging.getLogger(__name__).exception("Could not start the missing-browser fallback")
        except (OSError, ValueError, TimeoutError):
            if action == "start":
                with self.lock:
                    try:
                        resolved = self._resolve_call(logical_identifier)
                    except ValueError:
                        resolved = None
                    disconnected = identifier.partition(":")[0] not in self.browsers
                    if (
                        disconnected
                        and call_version == self._call_versions.get(logical_identifier, 0)
                        and not self._stopped_identity(expected)
                    ):
                        if expected is not None:
                            self._clear_attempt(identity=expected)
                        elif resolved is not None:
                            owner, _, tab = resolved.partition(":")
                            self._clear_attempt(self.browsers[owner], int(tab))
        finally:
            with self.lock:
                self._scheduled_calls.discard(logical_identifier)
                browser_id, _, tab = identifier.partition(":")
                if browser_id in self.browsers and tab.isdecimal():
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
                    browser = BrowserConnection(stream, packet.get("browserName"), packet.get("browserInstance"))
                    with self.lock:
                        self.browsers[browser.id] = browser
                    while (packet := read_packet(stream)) is not None:
                        self.update(browser, packet)
                elif packet.get("role") == "application":
                    self._application(connection, stream, packet.get("active", True), packet.get("generation", 0))
                elif packet.get("role") == "meter":
                    from .spectro_transport import serve_meter

                    serve_meter(self, connection, stream, packet.get("path"))
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
                                reason=packet.get("reason", "user"),
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
                            result = self.command(
                                packet.get("id"),
                                action,
                                microphone=packet.get("microphone", True),
                                user=True,
                                reason=packet.get("reason", "user"),
                            )
                    except (OSError, ValueError, TimeoutError) as error:
                        result = {"error": str(error)}
                    send_packet(stream, result)
        except (OSError, ValueError):
            pass
        finally:
            if browser:
                with self.lock:
                    self._remember_meeting_ids(browser)
                    owners = self._owners()
                    for tab in browser.meetings:
                        if owners.get((browser.browser_instance or browser.id, tab)) is not browser:
                            continue
                        state = browser.states.get(tab, {})
                        identifier = self._meeting_id(browser, tab)
                        capture_owner = self.captures.get(state.get("captureId"))
                        if (
                            (capture_owner is None or capture_owner.finished)
                            and not self._stopped_identity(self._expected_call(identifier))
                            and identifier not in self._scheduled_calls
                            and not any(
                                p.get("action") == "start" and p["tabId"] == tab for p in browser.pending.values()
                            )
                        ):
                            self._clear_attempt(browser, tab)
                    self._remember_control_loss(browser)
                    self._restore_stopped(browser, browser.meetings)
                    browser.closed = True
                    self.browsers.pop(browser.id, None)
                    for pending in browser.pending.values():
                        pending["event"].set()

    def _remember_control_loss(self, browser):
        if browser.browser_instance is None:
            return
        for tab, state in browser.states.items():
            capture_id = state.get("captureId")
            binding = self.call_captures.get((browser.browser_instance, tab))
            page = browser.page_states.get(tab)
            meeting = browser.meetings.get(tab)
            if (
                not state.get("recording")
                or binding is None
                or binding[0] != capture_id
                or page in ("closed", "navigating")
                or meeting is not None
                and meeting.get("sourceCount", 0) == 0
            ):
                continue
            if any(
                other is not browser
                and not other.closed
                and other.status_seen
                and other.browser_instance == browser.browser_instance
                and other.states.get(tab, {}).get("captureId") == capture_id
                for other in self.browsers.values()
            ):
                continue  # Retiring an old link must not lose the replacement's control.
            self._control_reconnects.setdefault(
                capture_id,
                ControlReconnect(
                    browser.browser_instance, tab, time.monotonic(), "page-state-v1" in browser.capabilities
                ),
            )
        while len(self._control_reconnects) > STOPPED_CALL_LIMIT:
            oldest = min(self._control_reconnects, key=lambda key: self._control_reconnects[key].since)
            self._control_reconnects.pop(oldest)

    def _bind_capture(self, browser, tab, capture_id):
        if browser.browser_instance is None:
            return
        key = (browser.browser_instance, tab)
        previous = self.call_captures.get(key)
        url = (
            previous[1]
            if previous is not None and previous[0] == capture_id and previous[1] is not None
            else browser.meetings.get(tab, {}).get("url")
        )
        self.call_captures[key] = (capture_id, url)
        owner = self.captures.get(capture_id)
        identity = (browser.browser_instance, tab, url)
        if owner is not None:
            owner.call_identity = identity
        reason = self._capture_stop_reason(browser, tab, identity)
        if reason is not None:
            self._cancel_capture(capture_id, identity, reason)

    def _store_stop(self, key, url):
        # An unknown URL must never suppress an unrelated call in this tab.
        if url is None:
            return
        self.stopped_calls[key] = (url, time.monotonic())
        while len(self.stopped_calls) > STOPPED_CALL_LIMIT:
            oldest = min(self.stopped_calls, key=lambda key: self.stopped_calls[key][1])
            old_url, _seen = self.stopped_calls.pop(oldest)
            self._clear_attempt(identity=(*oldest, old_url))

    def _remember_stop(self, browser, tab, stopped):
        """Remember a call the user stopped; starting it again forgets that."""
        if browser.browser_instance is None:
            return
        key = (browser.browser_instance, tab)
        if not stopped:
            previous = self.stopped_calls.pop(key, None)
            if previous is not None:
                self._clear_attempt(browser, tab, identity=(*key, previous[0]))
            return
        bound = self.call_captures.get(key)
        self._store_stop(key, bound[1] if bound else browser.meetings.get(tab, {}).get("url"))

    def _remember_capture_stop(self, capture):
        """Remember the original call even when its browser link is down."""
        with self.lock:
            for key, (capture_id, url) in self.call_captures.items():
                if capture_id == capture.capture_id:
                    self._store_stop(key, url)

    def _expire_stopped(self, now):
        # Run against the prior live inventory, before an incoming report can
        # revive a stop whose browser has been absent longer than the grace.
        reported = {(b.browser_instance, tab) for b in self.browsers.values() if not b.closed for tab in b.meetings}
        for key, (url, seen) in list(self.stopped_calls.items()):
            if key not in reported and now - seen >= STOPPED_CALL_GRACE:
                del self.stopped_calls[key]
                self._clear_attempt(identity=(*key, url))

    def _restore_stopped(self, browser, previous):
        """Restore only the same call, within its reconnect/report grace."""
        instance = browser.browser_instance
        if instance is None:
            return
        now = time.monotonic()
        for tab in previous.keys() - browser.meetings.keys():
            lineage = self._call_lineage.get((instance, tab))
            if lineage is None or lineage[1] <= browser.connected_at:
                self.stopped_calls.pop((instance, tab), None)
                self._clear_attempt(browser, tab, identity=(instance, tab, previous[tab]["url"]))
        for tab, meeting in browser.meetings.items():
            lineage = self._call_lineage.get((instance, tab))
            if lineage is not None and lineage[1] > browser.connected_at and lineage[0] != meeting["url"]:
                continue
            stopped = self.stopped_calls.get((instance, tab))
            if stopped is None:
                continue
            late = min(now - browser.connected_at, now - stopped[1]) >= STOPPED_CALL_REPORT_WINDOW
            if stopped[0] != meeting["url"] or tab not in previous and late:
                del self.stopped_calls[(instance, tab)]
                self._clear_attempt(browser, tab, identity=(instance, tab, stopped[0]))
                continue
            self.stopped_calls[(instance, tab)] = (meeting["url"], now)
            browser.attempted.add(tab)

    def serve(self, path, ready=None, *, idle_timeout=None):
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
                idle_since = time.monotonic()
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
                            idle_since = time.monotonic()
                            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()
                        except TimeoutError:
                            pass
                        # UI/PCM traffic may keep accept() busy indefinitely;
                        # call-end and return deadlines must still advance.
                        self.policy()
                        if idle_timeout is not None:
                            with self.lock:
                                active = (
                                    self.browsers
                                    or self.applications
                                    or any(not capture.finished for capture in self.captures.values())
                                )
                            if active:
                                idle_since = time.monotonic()
                            elif time.monotonic() - idle_since >= idle_timeout:
                                break
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
    parser.add_argument("--idle-timeout", type=float)
    args = parser.parse_args()
    if args.idle_timeout is not None and args.idle_timeout <= 0:
        parser.error("idle timeout must be positive")
    BrowserService(require_application=True).serve(args.serve, idle_timeout=args.idle_timeout)


if __name__ == "__main__":
    main()
