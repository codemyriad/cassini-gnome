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
from collections import deque
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from .call_lifecycle import (
    BROWSER_RETURN_GRACE,
    CALL_END_GRACE,
    CONTROL_RECONNECT_GRACE,
    CallLifecycle,
    ResumeInputs,
)
from .refusals import NothingOpened, StartCancelled, word
from .session_clock import SessionClock
from .session_events import source_state

MAX_PACKET = 512 * 1024
# A start with no expected call generation (only a D17 retry carries one).
ANY_GENERATION = object()
RESUME_CAPTURE = "resume-capture-v1"
# Bounded correlation of in-flight resume asks per control connection.
RESUME_HISTORY = 64
# A call the user stopped stays stopped beyond the control link that carried
# the stop, so a reconnect does not start it again automatically.
STOPPED_CALL_GRACE = 10 * 60
STOPPED_CALL_LIMIT = 256
# A new extension worker reports the calls already open as its pages reconnect.
STOPPED_CALL_REPORT_WINDOW = 30
# A peer that stops reading must not hold a writer, or the commands behind it.
WRITE_DEADLINE = 10
OUTBOUND_LIMIT = 64
# A frozen companion keeps its audio socket open. A healthy one can leave an
# open remote span silent while a failed recorder waits to reattach: capture.js
# retryDelay() backs off 1, 5, 15, then 30 s, and the broker cannot see that
# state. The longest retry wait plus the extension's 10 s acknowledgement limit
# is 40 s, so 5 s of margin on top (D52.5), still under the 60 s span cut.
PCM_STALE_AFTER = 45
# heartbeat-v1 (D52.4): the app pings a negotiated control link, and any
# inbound frame proves it alive. Two missed pings plus 5 s of slack.
HEARTBEAT_INTERVAL = 10
CONTROL_LOSS_AFTER = 25


@dataclass(frozen=True)
class ControlReconnect:
    instance: str
    tab: int
    since: float
    lifecycle: bool


class StartRejected(ValueError):
    """The extension finished rejecting Start, rather than an unknown/late result."""

    def __init__(self, message, result=None):
        from .refusals import from_result

        super().__init__(message)
        # D58: the browser refusal it records; a pattern-valid result code is its detail.
        self.refusal = from_result(result)


class StartRefused(ValueError):
    """A Start that recorded nothing, with the refusals the start banner shows (D58)."""

    def __init__(self, message, refusals):
        super().__init__(message)
        self.refusals = list(refusals)


# A newer companion reconnects within 1–30 s and on a one-minute alarm; past
# that without another newer hello, it went away.
APP_TOO_OLD_S = 65

# D58 startFailed details for local refusals and unanswered Starts (banner only).
LOCAL_DETAILS = {
    "The browser disconnected": "disconnected",
    "A recording command is still being processed": "busy",
    "This meeting is already being recorded": "busy",
    "This meeting is no longer available": "gone",
}


def resumable(current, capture):
    """D52.5 system/lost, or the D58 #2 stand-in: system/missing with the browser lost (R82 1a)."""
    source = (current.source, current.reason)
    return source == ("system", "lost") or source == ("system", "missing") and capture.browser_lost


def start_refusals(error):
    """The refusals a failed Start shows in its banner. A cancelled Start shows none."""
    from .refusals import entry

    if isinstance(error, (StartRefused,)) or hasattr(error, "refusals"):
        return list(error.refusals)
    if isinstance(error, StartRejected):
        return [error.refusal]
    message = str(error)
    if isinstance(error, ValueError) and message == "Recording start was cancelled":
        return []
    detail = "timeout" if isinstance(error, TimeoutError) else LOCAL_DETAILS.get(message)
    # Nobody refused: no answer, or this app refused locally. No raw text to keep.
    return [entry(0, "browser", "startFailed", detail=detail)]


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


def encode_packet(value):
    data = json.dumps(value, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_PACKET:
        raise ValueError("Control packet is too large")
    return data


def send_packet(stream, value):
    stream.write(encode_packet(value))
    stream.flush()


def connect(path=None, *, timeout=30):
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(timeout)
    connection.connect(str(path or service_path()))
    return connection


# A connect that finds no broker: its socket is gone (one that exits unlinks
# it) or nothing listens on it (one that crashed left it behind). A broker that
# listens, however stuck, never refuses: a full backlog fails with EAGAIN.
BROKER_ABSENT = (FileNotFoundError, ConnectionRefusedError)


def ensure_service(path=None, *, cancelled=None, absent=None):
    """The broker's socket, spawning a broker if none answers. absent(error),
    if given, hears at once that the first connect found no broker
    (BROKER_ABSENT), before the spawn and its wait of up to 5 s: an exiting
    broker still holds its lock while it stops its captures, so the new one
    may not take over within it."""
    target = Path(path or service_path())
    try:
        connection = connect(target, timeout=0.5)
        connection.close()
        return target
    except BROKER_ABSENT as error:
        if absent is not None:
            absent(error)
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


class Transport:
    """A peer socket that a watchdog can close without taking its writer's locks."""

    def __init__(self, connection, clock):
        self.connection = connection
        self.clock = clock
        self.write_started = None
        self.shut_down = threading.Event()

    def expired(self, now, *, wait=True):
        """Why this peer must be closed now, or None. wait=False never waits
        for a capture's lock (the broker's tick: #230)."""
        started = self.write_started
        if started is not None and now - started >= WRITE_DEADLINE:
            return "stopped reading"
        return None

    def shutdown(self):
        # Never close here: the reader thread still owns the descriptor. Its
        # blocked read sees EOF and a blocked write fails, so both unwind.
        self.shut_down.set()
        if self.connection is not None:
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)


class AudioTransport(Transport):
    def __init__(self, connection, clock):
        super().__init__(connection, clock)
        self.capture = None
        # Any packet counts (PCM, track, sources, trackEnd), so declaring a new
        # span restarts the wait.
        self.last_inbound = clock()
        # While the reader handles a packet, a quiet peer is the broker's own
        # stall (a disk write under the capture lock), never the browser's.
        self.handling = False
        # Wire span ids declared on this transport; they end when it closes.
        self.spans = set()

    def expired(self, now, *, wait=True):
        capture = self.capture
        if (
            capture is not None
            and not self.handling
            and capture.multistream_capable
            and now - self.last_inbound >= PCM_STALE_AFTER
            and not (capture.finished or capture.stopping or capture.stop_requested.is_set())
            # A busy capture lock (None) is the broker's own stall, not the
            # peer's: the next tick decides.
            and capture.remote_open(wait=wait)
        ):
            # Only a running remote recorder must keep sending. Before the first
            # PCM, or after every span ended (a guest left, the receiver dropped
            # out during a network blip), a quiet transport is still healthy.
            # A control-loss fallback is no exception (D57): a frozen
            # companion's attached transport is closed, so a resume can follow.
            return "stopped sending audio"
        return super().expired(now, wait=wait)


class BrowserConnection(Transport):
    def __init__(self, stream, browser_name=None, browser_instance=None, *, connection=None, clock=time.monotonic):
        super().__init__(connection, clock)
        self.browser_name = browser_name if isinstance(browser_name, str) and len(browser_name) <= 100 else None
        self.browser_instance = (
            browser_instance if isinstance(browser_instance, str) and 0 < len(browser_instance) <= 128 else None
        )
        self.id = str(uuid4())
        self.stream = stream
        # Orders a command's validity check with its place in the queue. No
        # socket I/O happens under it; one writer thread owns the stream.
        self.order_lock = threading.RLock()
        self.outbound = deque()
        self.outbound_ready = threading.Condition(threading.RLock())
        self.writer = None
        self.ignored = set()
        self.last_inbound = clock()
        # Set by the first status after a heartbeat-v1 hello; None never pings.
        self.last_ping = None
        self.ping_seq = 0
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
        # tab -> captureId of the extension's own sessions in its latest raw
        # inventory, retained or stale-generation rows included. A lost
        # recording's tab is released only once its session is gone (D52.5).
        self.reported = {}
        # requestId -> captureId of resume-capture-v1 asks on this connection.
        self.resumes = {}
        self.closed = False

    def expired(self, now, *, wait=True):
        if self.last_ping is not None and now - self.last_inbound >= CONTROL_LOSS_AFTER:
            return "stopped answering pings"
        return super().expired(now, wait=wait)

    def send(self, packet):
        data = encode_packet(packet)
        with self.outbound_ready:
            if self.shut_down.is_set():
                raise OSError("The browser connection is closed")
            if len(self.outbound) >= OUTBOUND_LIMIT:
                self.shutdown()
                raise OSError("The browser stopped reading commands")
            self.outbound.append(data)
            if self.writer is None:
                self.writer = threading.Thread(target=self._write, daemon=True, name="cassini-browser-writer")
                self.writer.start()
            self.outbound_ready.notify()

    def shutdown(self):
        super().shutdown()
        with self.outbound_ready:
            self.outbound.clear()
            self.outbound_ready.notify()

    def _write(self):
        while True:
            with self.outbound_ready:
                while not self.outbound and not self.shut_down.is_set():
                    self.outbound_ready.wait()
                if self.shut_down.is_set():
                    return
                data = self.outbound.popleft()
                self.write_started = self.clock()
            try:
                self.stream.write(data)
                self.stream.flush()
            except (OSError, ValueError):
                self.shutdown()
                return
            finally:
                self.write_started = None


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
        clock=time.monotonic,
    ):
        self.config = (
            Path(config)
            if config
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cassini/browser.json"
        )
        self.auto_record = False
        self.auto_record_revision = time.monotonic_ns()
        # Explicit auto opt-in supersedes already admitted Stops, including
        # saves whose publication/finished callback has not reached the broker.
        self._superseded_user_stops = set()
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.auto_record = json.loads(self.config.read_text()).get("autoRecord") is True
        self.lock = threading.RLock()
        # Transport liveness only; lifecycle timers keep their own clock.
        self.clock = clock
        # Command and batch deadlines (Start, Stop, reconnect grace). Waits
        # still sleep in real time and recheck this clock when they wake.
        self.deadline_clock = time.monotonic
        self.browsers = {}
        self._audio_transports = set()
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
        self._retired_generations = {}
        self._pending_calls = {}
        self._cancelled_captures = {}
        self._cancel_capture_pending = set()
        # The document changes a control packet reports, per reader thread:
        # applied to their captures once the broker's lock is released (#230).
        self._documents = threading.local()
        self._capture_callbacks = threading.local()  # Resume refusal checkpoints also wait off this lock.
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
        # D58 point 5: when a hello last asked for a newer control version than ours.
        self.app_too_old = None

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
        owner.request_stop(reason=reason, user_stopped=self._stopped_identity(identity) and reason == "user")
        if capture_id not in self._cancel_capture_pending:
            self._cancel_capture_pending.add(capture_id)
            self.workers.submit(self._finish_cancelled_capture, capture_id, owner, reason)

    def _finish_cancelled_capture(self, capture_id, owner, reason):
        try:
            owner.stop(reason=reason, user_stopped=False)
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
            owner.stop(reason=reason, user_stopped=metadata.get("user_stopped", False))
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
        asked = browser.resumes.pop(request_id, None) if isinstance(request_id, str) else None
        if asked is not None:
            # D52.5: a resume result is informational. Success is only ever the
            # native attach plus PCM; a refusal changes nothing and is never
            # retried here, nor turned into a fresh Start. Its status is not
            # this connection's inventory, and a stale-result cancel would
            # cancel the very recording it named.
            if packet.get("ok") is not True:
                logging.getLogger(__name__).info(
                    "The browser did not resume recording %s: %s", asked, str(packet.get("error"))[:500]
                )
                with self.lock:
                    capture = self.captures.get(asked)
                if capture is not None:
                    # D58 #10 (R82 1b): the host's result code is the message.
                    code = packet.get("code")
                    refusal = (capture, code if word(code) else None, request_id)
                    pending = getattr(self._capture_callbacks, "pending", None)
                    if pending is not None:
                        pending.append(refusal)
                    else:
                        capture.refuse_resume(None, refusal[1], request_id=request_id)
            return
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
                    # A retained generation is not news about that document.
                    state.pop("documentGeneration", None)
                    if other_tab in browser.page_states:
                        state["pageState"] = browser.page_states[other_tab]
                    preserved.append(state)
                # GNOME's own retained rows: not the extension's inventory.
                self._status(browser, dict(status, sessions=[*preserved, target]), inventory=False)
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

    @staticmethod
    def _validate_generation(browser, tab, generation):
        """A D17 retry starts only the call its line was for: a rejoin is another call."""
        if generation is not ANY_GENERATION and browser.meetings.get(tab, {}).get("callGeneration") != generation:
            raise ValueError("This meeting is no longer available")

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

    def _capture_phase(self, browser, tab):
        """One per-capture state for the app's Record toggle and for start_all.

        'recording', 'stopping' or None. A capture that stops (Stop requested
        or sent, PCM ending, publication running) is 'stopping': a new session
        may start beside it (D18). A raw row with no capture of ours still
        counts as recording unless its Stop is pending (D52.5: never twice).
        Caller holds self.lock.
        """
        raw = browser.states.get(tab) or {}
        owner = self.captures.get(raw.get("captureId"))
        if owner is not None and not owner.finished:
            return "stopping" if owner.stopping or owner.stop_requested.is_set() else "recording"
        if not raw.get("recording"):
            return None
        if raw.get("stopping") is True or any(
            p.get("action") == "stop" and p.get("tabId") == tab for p in browser.pending.values()
        ):
            return "stopping"
        return "recording"

    def _stop_pending(self, identifier):
        """Whether a Stop for this meeting, or a Stop-all, is still unanswered. Caller holds self.lock."""
        return self._stopping_all.is_set() or any(
            p.get("action") == "stop" and self._meeting_id(b, p["tabId"]) == identifier
            for b in self.browsers.values()
            for p in b.pending.values()
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
            min(deadline, self.deadline_clock() + CONTROL_RECONNECT_GRACE)
            if deadline is not None
            else self.deadline_clock() + CONTROL_RECONNECT_GRACE
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
            if not wait or self.deadline_clock() >= deadline or self.closed.wait(0.02):
                raise ValueError("This browser is no longer connected")

    def _recover_command(self, browser, tab, action, pending, deadline):
        if browser.browser_instance is None:
            return None
        with self.lock:
            identifier = self._meeting_id(browser, tab)
        deadline = min(deadline, self.deadline_clock() + CONTROL_RECONNECT_GRACE)
        while not self.closed.is_set() and self.deadline_clock() < deadline:
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
                    value["phase"] = self._capture_phase(browser, tab)
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
                appTooOld=self.app_too_old is not None and self.clock() - self.app_too_old <= APP_TOO_OLD_S,
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
            self.workers.submit(capture.stop, capture.error, user_stopped=False)
        elif event == "finished" and (capture.directory is not None or getattr(capture, "nothing_recorded", False)):
            # D58.1: a recording nothing ever opened into ends without a folder
            # at Stop; its tab must not stay "recording".
            with self.lock:
                self._control_reconnects.pop(capture.capture_id, None)
                for (instance, tab), (capture_id, url) in self.call_captures.items():
                    if capture_id == capture.capture_id:
                        capture.call_identity = (instance, tab, url)
                        if (
                            getattr(capture, "user_stopped", False)
                            and capture.capture_id not in self._superseded_user_stops
                        ):
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
                    self._superseded_user_stops.discard(key)
            if capture.directory is not None:
                self._remember_saved(str(capture.directory), capture.host.session.manifest["title"])

    def _remember_saved(self, path, title, message=None):
        with self.lock:
            recording = dict(path=path, title=title, message=message or "Recording saved")
            self.recordings = [recording] + [r for r in self.recordings if r["path"] != path][:19]

    def _resume_attach(self, packet, transport):
        """D52.3c: attach this new audio transport to an active recording.

        Returns (target, proof). Every check passes, or ResumeRefused is raised
        and nothing changed: the caller's capture stays None, so its teardown
        can neither stop nor close the target.
        """
        from .browser_capture import RESUME_FEATURE, ResumeRefused, generation_value, utf8_length

        identifier = packet.get("captureId")
        origin = packet.get("timelineOriginMs")
        features = packet.get("features")
        # R82 1b: the control command's requestId, copied onto the native resume.
        request_id = packet.get("requestId")
        if (
            type(packet.get("version")) is not int
            or packet["version"] != 1
            or packet.get("independent") is not True
            or not isinstance(identifier, str)
            or not 0 < utf8_length(identifier) <= 128
            or type(origin) is not int
            or not 0 <= origin <= 253402300799000
            or generation_value(packet.get("callGeneration")) is None
            or not isinstance(features, list)
            or len(features) > 8
            or any(not isinstance(f, str) or len(f) > 64 for f in features)
            or len(set(features)) != len(features)
            or RESUME_FEATURE not in features
            or "requestId" in packet
            and not (
                isinstance(request_id, str)
                and 0 < len(request_id) <= 128
                or type(request_id) is int
                and 0 <= request_id <= 2**53 - 1
            )
        ):
            raise ResumeRefused("Invalid resume request")
        with self.lock:
            target = self.captures.get(identifier)
            refused = target is None or (
                identifier in self._cancelled_captures
                or self._stopping_all.is_set()
                or any(
                    capture_id == identifier and self.stopped_calls.get(key, (None,))[0] == url
                    for key, (capture_id, url) in self.call_captures.items()
                    if url is not None
                )
                or any(t.capture is target for t in self._audio_transports if t is not transport)
            )
        # Lock order is service, then capture: never wait on the capture's
        # transition (a committing fallback) while holding the inventory lock.
        try:
            if refused:
                raise ResumeRefused()
            proof = target.resume(packet)
        except ResumeRefused as error:
            if target is not None and not refused:
                # D58 #10: in the existing folder, at its real time, when the
                # recording still waits on system for that call (refuse_resume).
                target.refuse_resume(packet, str(error), request_id=request_id)
            raise
        try:
            with self.lock:
                transport.capture = target
        except BaseException:
            target.close()  # Never leave an attachment nothing owns.
            raise
        return target, proof

    def _audio(self, stream, connection=None):
        from .browser_capture import BrowserCapture, ResumeRefused
        from .recording_session import ProtocolError

        capture = None
        problem = None
        transport = AudioTransport(connection, self.clock)

        def reply(packet):
            transport.write_started = self.clock()
            try:
                send_packet(stream, packet)
            finally:
                transport.write_started = None

        with self.lock:
            self._audio_transports.add(transport)
        try:
            reply(dict(ready=True, protocol=2))
            while (packet := read_packet(stream)) is not None:
                # D52.5: a frame arriving at or after PCM_STALE_AFTER is too
                # late, even when no tick has noticed the deadline yet.
                if (reason := transport.expired(self.clock())) is not None:
                    logging.getLogger(__name__).warning("Closing a browser peer that %s", reason)
                    transport.shutdown()
                    break
                transport.last_inbound = self.clock()
                transport.handling = True
                try:
                    if capture is None and packet.get("type") == "resume":
                        capture, result = self._resume_attach(packet, transport)
                    elif capture is None:
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
                            transport.capture = capture
                        result = capture.start(packet)
                    else:
                        result = capture.handle(packet)
                        if packet.get("type") == "track" and isinstance(packet.get("id"), str):
                            transport.spans.add(packet["id"])
                except Exception as error:
                    problem = str(error)
                    code = (
                        error.code
                        if isinstance(error, ProtocolError)
                        else "io"
                        if isinstance(error, OSError)
                        else "state"
                    )
                    failure = dict(type="error", code=code, message=str(error))
                    if isinstance(error, ResumeRefused):
                        # Command scoped: the extension closes only this attempt
                        # and never reports it as the target's failure.
                        failure["scope"] = "command"
                    # Keep the original failure if its error reply is lost.
                    with contextlib.suppress(ConnectionError):
                        reply(failure)
                    break
                try:
                    reply(result)
                except ConnectionError:
                    # PCM/metadata was accepted; losing its acknowledgement
                    # is transport loss, just like EOF. Preserve the native
                    # microphone and invoke Q4 rather than ending the meeting.
                    break
                # The peer could not send while this packet was handled.
                transport.last_inbound = self.clock()
                transport.handling = False
        finally:
            self._audio_closed(transport, capture, problem)

    def _audio_closed(self, transport, capture, problem=None):
        """An audio transport ended (EOF, error, or a deadline shut it down).

        A clean loss keeps the recording on its native fallback (D33/D52.4);
        a protocol failure or the broker's own exit finalizes it.
        """
        with self.lock:
            self._audio_transports.discard(transport)
        if capture is None:
            return
        try:
            if problem or self.closed.is_set():
                capture.stop(problem, reason="error" if problem else "appQuit", user_stopped=False)
            else:
                try:
                    capture.retire_transport_spans(transport.spans)
                except Exception:
                    logging.getLogger(__name__).exception("Could not retire a closed transport's spans")
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
                self._superseded_user_stops.intersection_update(self.captures)
                self._superseded_user_stops.update(
                    key
                    for key, capture in self.captures.items()
                    if getattr(capture, "stop_requested", None) is not None and capture.stop_requested.is_set()
                )
                self.stopped_calls.clear()
                self._attempted_calls.clear()
                for browser in self.browsers.values():
                    browser.attempted.clear()
        self.policy()
        return self.snapshot()

    def _status(self, browser, value, *, inventory=True):
        if type(value.get("recording")) is not bool:
            return
        browser.status_seen = True
        previous = browser.states
        received_pages = {}
        received_documents = {}
        browser.status = {
            key: value[key]
            for key in (
                "recording",
                "tabId",
                "path",
                "message",
                "tracks",
                "stopping",
                "captureId",
                "pageState",
                "callGeneration",
                "documentGeneration",
            )
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
                if self._stale_call_generation(browser, tab, item):
                    if tab in previous:
                        states[tab] = dict(previous[tab])
                    continue
                if item.get("pageState") in ("present", "navigating", "closed"):
                    received_pages[tab] = item["pageState"]
                if "documentGeneration" in item:
                    received_documents[tab] = item["documentGeneration"]
                if tab in states or not 0 <= tab <= 9007199254740991:
                    raise ValueError("Invalid or duplicate recording tab")
                states[tab] = {
                    key: item[key]
                    for key in (
                        "recording",
                        "tabId",
                        "path",
                        "message",
                        "tracks",
                        "stopping",
                        "captureId",
                        "pageState",
                        "callGeneration",
                        "documentGeneration",
                    )
                    if key in item
                }
            # Every raw session counts, stale-generation rows included: the
            # extension still holds that tab until its session is deleted. A
            # synthesized inventory (a retired command's late result, merged
            # with GNOME's retained rows) says nothing about that.
            if inventory:
                browser.reported = {item["tabId"]: item.get("captureId") for item in items if item["recording"]}
        else:
            tab = value.get("tabId")
            if not inventory:
                pass
            elif type(tab) is int and "independent-sessions-v1" in browser.capabilities:
                if value["recording"]:
                    browser.reported[tab] = value.get("captureId")
                else:
                    browser.reported.pop(tab, None)
            else:
                browser.reported = {tab: value.get("captureId")} if value["recording"] and type(tab) is int else {}
            if type(tab) is int and self._stale_call_generation(browser, tab, value):
                return
            if type(tab) is int and value.get("pageState") in ("present", "navigating", "closed"):
                received_pages[tab] = value["pageState"]
            if type(tab) is int and "documentGeneration" in value:
                received_documents[tab] = value["documentGeneration"]
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
                if self._still_recording(owner) and str(owner.directory) == item.get("path"):
                    states.setdefault(item["tabId"], dict(item, recording=True))
                    browser.attempted.add(item["tabId"])
        for tab, old in previous.items():
            owner = self.captures.get(old.get("captureId"))
            if self._still_recording(owner) and owner.browser_lost:
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
                    and self._still_recording(owner)
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
        self._adopt_by_call(browser, states)
        for tab, state in states.items():
            owner = self.captures.get(state.get("captureId"))
            if owner is not None and not owner.finished and state.get("recording"):
                owner.call_tab = tab  # Under any peer, with or without browserInstance.
        for tab in received_pages.keys() | received_documents.keys():
            owner = self.captures.get(states.get(tab, previous.get(tab, {})).get("captureId"))
            if owner is not None and not owner.finished:
                self._observe_document(owner, received_pages.get(tab), received_documents.get(tab))
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

    def _adopt_by_call(self, browser, states):
        """D52.5: a peer's call row finds its unfinished recording by call identity.

        callGeneration is browser-generated and unguessable, so (tabId,
        callGeneration) names one call under any control peer, past or
        present, with or without browserInstance. The adopted row shows Stop,
        Record all and Start skip it, and policy finds this peer for it.
        """
        for key, capture in self.captures.items():
            tab = getattr(capture, "call_tab", None)
            generation = getattr(capture, "call_generation", None)
            if (
                tab is None
                or generation is None
                or not self._still_recording(capture)
                or capture.directory is None
                or key in self._cancelled_captures
                or browser.meetings.get(tab, {}).get("callGeneration") != generation
            ):
                continue
            state = states.setdefault(tab, dict(recording=True, tabId=tab, captureId=key, path=str(capture.directory)))
            if state.get("captureId") == key:
                browser.attempted.add(tab)

    def _observe_document(self, owner, page, document):
        """T1 (navigating) and T2 (a new documentGeneration) for a recording's tab.

        The same rules as the shared oracle's documentChanged frame, for any
        reported pageState (closed included) and generation.
        """
        if page not in ("navigating", "present") and document is None:
            return
        pending = getattr(self._documents, "pending", None)
        if pending is not None:
            # Under the broker's lock (update()): retiring spans takes the
            # capture's lock, which a stalled checkpoint can hold, and a Stop
            # pressed meanwhile needs the broker's lock to be marked (#230).
            pending.append((owner, page, document))
            return
        retired = owner.observe_document(document, navigating=page == "navigating", present=page == "present")
        if retired:
            logging.getLogger(__name__).debug("Retired %d spans of a gone document from the PCM watchdog", retired)

    @staticmethod
    def _still_recording(capture):
        """An owned capture still recording, not one stopped and only saving.

        A stopped managed capture publishes in the background (stopping, not
        finished), as BrowserCapture.snapshot reports: it must not be shown
        or treated as recording its call (#237).
        """
        return (
            capture is not None
            and not capture.finished
            and not getattr(capture, "stopping", False)
            and not (getattr(capture, "stop_requested", None) is not None and capture.stop_requested.is_set())
        )

    def _generation_recording(self, tab, meeting, *, active=False):
        """Whether an unfinished capture already records this row's call.

        Peer matching falls back to the connection id when a companion sends
        no browserInstance, so a returning companion must not start a second
        folder for a call (tabId plus callGeneration, D52.5) that is still
        being recorded under any control peer.
        """
        generation = meeting.get("callGeneration")
        if generation is None:
            return False
        # A capture's tab: its instance binding, or the tab a status last
        # bound it to under any peer.
        bindings = {(capture_id, bound) for (_owner, bound), (capture_id, _url) in self.call_captures.items()}
        bindings |= {(capture_id, c.call_tab) for capture_id, c in self.captures.items() if c.call_tab is not None}
        return any(
            bound == tab
            and (capture := self.captures.get(capture_id)) is not None
            and capture.call_generation == generation
            and not capture.finished
            # A recording the user stopped no longer blocks a fresh manual
            # Record (R67) while it saves.
            and not (active and (capture.stopping or capture.stop_requested.is_set()))
            for capture_id, bound in bindings
        )

    def _stale_call_generation(self, browser, tab, value):
        generation = value.get("callGeneration")
        capture_id = value.get("captureId")
        binding = self.call_captures.get((browser.browser_instance or browser.id, tab))
        if (
            isinstance(capture_id, str)
            and capture_id in self._cancelled_captures
            and binding
            and binding[0] != capture_id
        ):
            return True
        return (
            isinstance(generation, str)
            and (browser.browser_instance or browser.id, tab, generation) in self._retired_generations
        )

    def _retire_changed_call(self, browser, tab, previous, current):
        """Fence old PCM now; the worker finalizes without the inventory lock."""
        instance = browser.browser_instance or browser.id
        if self._owners().get((instance, tab)) is not browser:
            return
        binding = self.call_captures.get((instance, tab))
        capture_id = binding[0] if binding else browser.states.get(tab, {}).get("captureId")
        capture = self.captures.get(capture_id)
        active = (
            capture_id is not None
            and capture_id not in self._cancelled_captures
            and (capture is None or not capture.finished and not capture.stop_requested.is_set())
        )
        old_generation = (getattr(capture, "call_generation", None) if active else None) or previous.get(
            "callGeneration"
        )
        new_generation = current.get("callGeneration")
        old_url = binding[1] if active and binding and binding[1] is not None else previous.get("url")
        changed = (
            old_url is not None
            and old_url != current["url"]
            or (old_generation is not None and new_generation is not None and old_generation != new_generation)
        )
        if not changed:
            return
        if old_generation is not None:
            self._retired_generations[(instance, tab, old_generation)] = None
            while len(self._retired_generations) > 1024:
                self._retired_generations.pop(next(iter(self._retired_generations)))
        identity = (instance, tab, old_url)
        logical = self._logical_id(identity) if browser.browser_instance is not None else f"{browser.id}:{tab}"
        self._cancel_call(logical)
        self._clear_attempt(browser, tab)
        if active:
            self._cancel_capture(capture_id, identity, "callEnded")

    def update(self, browser, packet):
        self._documents.pending = documents = []
        self._capture_callbacks.pending = callbacks = []
        try:
            self._update(browser, packet)
        finally:
            # In report order, on this peer's reader thread, outside the
            # broker's lock: they may wait for a stalled capture lock (#230).
            self._documents.pending = None
            self._capture_callbacks.pending = None
            try:
                for owner, page, document in documents:
                    self._observe_document(owner, page, document)
            finally:
                for capture, code, request_id in callbacks:
                    capture.refuse_resume(None, code, request_id=request_id)
        self.policy()

    def _update(self, browser, packet):
        with self.lock:
            browser.last_inbound = self.clock()  # Every frame type proves liveness.
            kind = packet.get("type")
            if kind == "meetings":
                values = packet.get("meetings")
                if not isinstance(values, list) or len(values) > 100:
                    raise ValueError("Invalid meeting inventory")
                meetings = {}
                fresh = set()
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
                    tab = value["tabId"]
                    if self._stale_call_generation(browser, tab, value):
                        if tab in browser.meetings:
                            meetings[tab] = dict(browser.meetings[tab])
                        continue
                    meetings[tab] = {key: value[key] for key in ("tabId", "title", "url", "sourceCount")}
                    fresh.add(tab)
                    for key in ("callGeneration", "documentGeneration"):
                        generation = value.get(key)
                        if isinstance(generation, str) and 0 < len(generation.encode("utf-8")) <= 200:
                            meetings[tab][key] = generation
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
                for tab, meeting in meetings.items():
                    self._retire_changed_call(browser, tab, browser.meetings.get(tab, {}), meeting)
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
                        if instance == browser.browser_instance and tab in meetings and self._still_recording(owner):
                            browser.states.setdefault(
                                tab, dict(recording=True, tabId=tab, captureId=capture_id, path=str(owner.directory))
                            )
                            browser.attempted.add(tab)
                self._adopt_by_call(browser, browser.states)
                for tab in fresh:
                    meeting = meetings[tab]
                    owner = self.captures.get(browser.states.get(tab, {}).get("captureId"))
                    if owner is None or owner.finished:
                        continue
                    # A row names its tab's top document. Its generation is
                    # only compared for the recording's own call.
                    same_call = (
                        owner.call_generation is not None and meeting.get("callGeneration") == owner.call_generation
                    )
                    self._observe_document(
                        owner, meeting.get("pageState"), meeting.get("documentGeneration") if same_call else None
                    )
                browser.attempted.intersection_update(meetings)
            elif kind == "status":
                if "heartbeat-v1" in browser.capabilities and browser.last_ping is None:
                    # Pings wait for the first status: hello, meetings, status.
                    browser.last_ping = browser.last_inbound
                self._status(browser, packet)
            elif kind == "result":
                self._command_result(browser, packet)
            elif kind == "pong":
                pass  # Liveness only, recorded above; seq needs no correlation.
            elif kind == "hello":
                version = packet.get("version")
                if type(version) is int and version > 1:
                    # D58 point 5: a newer companion is visible as appTooOld, not a
                    # silent close. It retries at most every minute; once it stops
                    # (removed or downgraded) the state expires on its own.
                    self.app_too_old = self.clock()
                    logging.getLogger(__name__).warning(
                        "Browser companion speaks control version %d; update Cassini", version
                    )
                    raise ValueError("Unsupported browser control version")
                if type(version) is not int or version != 1:
                    logging.getLogger(__name__).warning(
                        "Closing a browser control connection with hello version %r", str(version)[:32]
                    )
                    raise ValueError("Unsupported browser control version")
                self.app_too_old = None
                browser.hello_seen = True
                capabilities = packet.get("capabilities", [])
                browser.capabilities = (
                    {c for c in capabilities if isinstance(c, str)}
                    if isinstance(capabilities, list) and len(capabilities) <= 16
                    else set()
                )
                browser.last_ping, browser.ping_seq = None, 0
            else:
                # Forward compatible: a newer companion's message is not a
                # broken link. Log each new type once per connection.
                name = kind[:64] if isinstance(kind, str) else type(kind).__name__
                if name not in browser.ignored and len(browser.ignored) < 16:
                    browser.ignored.add(name)
                    logging.getLogger(__name__).info("Ignoring unknown browser control event: %s", name)
            browser.page_states = {
                tab: page
                for tab, page in browser.page_states.items()
                if tab in browser.states or tab in browser.meetings
            }

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
        call_generation=ANY_GENERATION,
    ):
        from .session_events import END_REASONS

        deadline = (
            min(deadline, self.deadline_clock() + self.command_timeout)
            if deadline is not None
            else self.deadline_clock() + self.command_timeout
        )
        if action == "start" and (self.deadline_clock() >= deadline or cancelled is not None and cancelled.is_set()):
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
                capture.request_stop(reason=reason, user_stopped=user and reason == "user")
                if user:
                    # In the same admission: an opt-in queued on the lock then
                    # orders after the whole Stop intent and supersedes it.
                    self._remember_capture_stop(capture)
            capture.stop(reason=reason, user_stopped=user and reason == "user")
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
                if local is not None:
                    local.request_stop(reason=reason, user_stopped=user and reason == "user")
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
            local.stop(reason=reason, user_stopped=user and reason == "user")
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
                or self.deadline_clock() >= deadline
                or cancelled is not None
                and cancelled.is_set()
            ):
                raise ValueError("Recording start was cancelled")
            independent = "independent-sessions-v1" in browser.capabilities
            if action == "start":
                if tab not in browser.meetings:
                    raise ValueError("This meeting is no longer available")
                self._validate_call(browser, tab, expected, version=call_version)
                self._validate_generation(browser, tab, call_generation)
                # D52.5/R67: never a second folder for a call being recorded,
                # also for a manual Start; one the user stopped no longer counts.
                owner = self.captures.get(browser.states.get(tab, {}).get("captureId"))
                if (
                    owner is not None
                    and not owner.finished
                    and not owner.stopping
                    and not owner.stop_requested.is_set()
                ) or self._generation_recording(tab, browser.meetings[tab], active=True):
                    raise ValueError("This meeting is already being recorded")
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
                    capture.request_stop(reason=reason, user_stopped=user and reason == "user")
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
                    user_stopped=user and reason == "user",
                    preserve_capture=preserve_capture,
                    captures=set(),
                    captureId=browser.states.get(tab, {}).get("captureId"),
                    callGeneration=browser.meetings.get(tab, {}).get("callGeneration"),
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
                    capture.request_stop(reason=reason, user_stopped=user and reason == "user")
        if joining is not None:
            if capture is not None and not preserve_capture:
                # Local save is authoritative. The existing browser Stop may
                # still be waiting for an unreachable page; do not keep the
                # user's Stop waiting after their audio is safely saved.
                capture.stop(reason=reason, user_stopped=user and reason == "user")
                return dict(self.snapshot(), stoppedPath=str(capture.directory))
            if not joining["event"].wait(min(self.command_timeout, max(0, deadline - self.deadline_clock()))):
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
                capture.stop(reason=reason, user_stopped=user and reason == "user")
            packet = dict(type="command", requestId=request_id, action=action, tabId=tab, mode="tracks")
            if independent:
                packet.update(independent=True, captureLocal=False, microphone=microphone)
                if "multi-stream-v1" in browser.capabilities:
                    if action == "start" and pending.get("callGeneration") is not None:
                        packet["callGeneration"] = pending["callGeneration"]
                    elif action == "stop" and pending.get("captureId") is not None:
                        packet["captureId"] = pending["captureId"]
                if "page-state-v1" in browser.capabilities:
                    if action == "start":
                        packet["keepOnNavigation"] = True
                    else:
                        packet["reason"] = reason
            with browser.order_lock:
                if (
                    action == "start"
                    and expected is not None
                    and (browser.browser_instance, tab, browser.meetings.get(tab, {}).get("url")) != expected
                ):
                    raise ValueError("This meeting is no longer available")
                if action == "start":
                    self._validate_generation(browser, tab, call_generation)
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
            if not pending["event"].wait(min(self.command_timeout, max(0, deadline - self.deadline_clock()))):
                raise TimeoutError("The browser did not finish the recording command")
            if action == "stop" and not preserve_capture:
                capture = self._command_capture(pending, capture, reason)
            result = pending["result"]
            if action == "start" and pending.get("cancelled"):
                raise ValueError("Recording start was cancelled")
            if result is None and browser.closed:
                result = self._recover_command(browser, tab, action, pending, deadline)
            if result is None or result.get("ok") is not True:
                message = str((result or {}).get("error", "The browser disconnected"))[:500]
                if result is not None and action == "start" and not pending.get("cancelled"):
                    raise StartRejected(message, result)
                raise ValueError(message)
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

    def _batch(self, jobs, deadline, *, cancelled=None, expired=None):
        """Run jobs until the deadline. `expired` is set when the deadline,
        not a caller, retired the jobs still running (D58: a Start that got
        no answer is a startFailed refusal, not a cancelled Record)."""
        if not jobs:
            return [], []
        executor = ThreadPoolExecutor(max_workers=min(32, len(jobs)), thread_name_prefix="cassini-browser-batch")
        futures = [(identifier, executor.submit(job)) for identifier, job in jobs]
        remaining = {future for _identifier, future in futures}
        try:
            while remaining and self.deadline_clock() < deadline and not (cancelled is not None and cancelled.is_set()):
                _done, remaining = wait(
                    remaining, timeout=min(0.05, max(0, deadline - self.deadline_clock())), return_when=FIRST_COMPLETED
                )
            if remaining and cancelled is not None:
                if expired is not None and not cancelled.is_set():
                    expired.set()
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

    def start_all(self, *, microphone=True, group_id=None, only=None):
        """Start every recordable meeting, or with `only` just that one (R82 §2).

        `only` is {id, callGeneration}: a D17 line's Try again. It starts that
        meeting only while it is still the same call; otherwise nothing.
        """
        if type(microphone) is not bool:
            raise ValueError("Invalid microphone selection")
        if only is not None and (
            not isinstance(only, dict)
            or not isinstance(only.get("id"), str)
            or not 0 < len(only["id"]) <= 512
            or only.get("callGeneration") is not None
            and not isinstance(only.get("callGeneration"), str)
        ):
            raise ValueError("Invalid meeting to retry")
        group = threading.Event()
        token = str(uuid4()) if group_id is None else group_id
        if not isinstance(token, str) or not 0 < len(token) <= 128:
            raise ValueError("Invalid recording operation")
        with self.lock:
            if token in self._cancelled_groups:
                group.set()
            owners = self._owners()
            targets, skipped = [], []
            for b in self.browsers.values():
                if not b.status_seen:
                    continue
                for tab, meeting in b.meetings.items():
                    if owners.get((b.browser_instance or b.id, tab)) is not b or meeting.get("sourceCount", 0) <= 0:
                        continue
                    # A stopping capture does not block a new session; the
                    # Start waits for its Stop below. D52.5: a call already
                    # being recorded, under any peer, is never started twice.
                    if self._capture_phase(b, tab) == "recording":
                        reason = "recording"
                    elif self._generation_recording(tab, meeting, active=True):
                        reason = "sameCall"
                    else:
                        targets.append((self._meeting_id(b, tab), dict(meeting)))
                        continue
                    skipped.append(
                        dict(
                            id=self._meeting_id(b, tab),
                            title=meeting.get("title"),
                            callGeneration=meeting.get("callGeneration"),
                            reason=reason,
                        )
                    )
            if only is not None:
                targets = [
                    (identifier, meeting)
                    for identifier, meeting in targets
                    if identifier == only["id"] and meeting.get("callGeneration") == only.get("callGeneration")
                ]
                skipped = [s for s in skipped if s["id"] == only["id"]]
            self._record_groups[token] = group
        deadline = self.deadline_clock() + self.batch_timeout

        # A retry's call generation holds through its Start and any stand-in.
        expected = {} if only is None else dict(call_generation=only.get("callGeneration"))

        def start_one(identifier, meeting):
            # Record right after Stop: the browser answers that Stop only when
            # its PCM ends, and refuses a Start for the tab until then. A later
            # Stop cancels this group, so the wait never outlives the user.
            while not group.is_set() and self.deadline_clock() < deadline:
                with self.lock:
                    if not self._stop_pending(identifier):
                        break
                if self.closed.wait(0.02):
                    break
            if group.is_set() or self._stopping_all.is_set() or self.deadline_clock() >= deadline:
                raise ValueError("Recording start was cancelled")
            cleanup_sent = False
            try:
                return self.command(
                    identifier,
                    "start",
                    microphone=microphone,
                    user=True,
                    deadline=deadline,
                    cancelled=group,
                    **expected,
                )
            except StartRejected as rejected:
                if group.is_set() or self._stopping_all.is_set() or self.deadline_clock() >= deadline:
                    raise ValueError("Recording start was cancelled") from None
                if rejected.refusal.get("detail") == "busy":
                    # D58 #11: it may be this tab already recording; never record it twice.
                    raise StartRefused(str(rejected), [rejected.refusal]) from None
                try:
                    return self.start_fallback(
                        identifier,
                        meeting,
                        microphone,
                        cancelled=group,
                        deadline=deadline,
                        refusals=[rejected.refusal],
                        **expected,
                    )
                except (NothingOpened, StartCancelled):
                    raise  # It already lists the browser refusal first, or none: a Stop.
                except (OSError, ValueError, TimeoutError) as error:
                    # The browser refused first: it leads the banner and its caption.
                    raise StartRefused(str(error), [rejected.refusal, *start_refusals(error)]) from error
            except TimeoutError:
                # No acknowledgement means Start may still be finishing in the
                # browser. Retire that attempt before its caller can retry it.
                cleanup_sent = True
                with contextlib.suppress(OSError, ValueError, TimeoutError):
                    self.command(identifier, "stop", user=True)
                raise
            finally:
                if not cleanup_sent and group.is_set() and self.deadline_clock() >= deadline:
                    # The request can return while a late accepted Start is
                    # being retired. Never leave that unacknowledged capture on.
                    with contextlib.suppress(OSError, ValueError, TimeoutError):
                        self.command(identifier, "stop", user=True)

        try:
            jobs = [
                (identifier, lambda identifier=identifier, meeting=meeting: start_one(identifier, meeting))
                for identifier, meeting in targets
            ]
            expired = threading.Event()
            results, _futures = self._batch(jobs, deadline, cancelled=group, expired=expired)
            titles = {identifier: meeting["title"] for identifier, meeting in targets}
            cancelled = group.is_set() and not expired.is_set()
            generations = {identifier: meeting.get("callGeneration") for identifier, meeting in targets}

            def reported(error):
                # D58: only the user's cancel shows no refusal. Without one, a
                # Start that saw the deadline itself (its own check, or the
                # cancel the expiry sets) before the results were read is
                # unanswered, like one still running then: startFailed/timeout.
                if not cancelled and not isinstance(error, StartCancelled) and not start_refusals(error):
                    return TimeoutError("The browser did not finish the recording command")
                return error

            failures = [
                dict(
                    id=identifier,
                    title=titles[identifier],
                    callGeneration=generations[identifier],
                    error=str(error),
                    refusals=start_refusals(error),
                )
                for identifier, error in ((i, reported(e)) for i, _result, e in results if e is not None)
            ]
            return dict(
                self.snapshot(), attempted=len(targets), skipped=skipped, cancelled=cancelled, failures=failures
            )
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
        refusals=(),
        call_generation=ANY_GENERATION,
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
                or (deadline is not None and self.deadline_clock() >= deadline)
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
            self._validate_generation(browser, tab, call_generation)
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
                capture_generation = browser.meetings.get(tab, {}).get("callGeneration")
                capture.call_tab = tab
                self.captures[capture_id] = capture
                browser.states[tab] = dict(recording=True, captureId=capture_id, tabId=tab)
                if browser.browser_instance is not None:
                    self._bind_capture(browser, tab, capture_id)
            else:
                capture = None
        if capture is None:
            return existing.snapshot()
        # GNOME picks this origin: it and its monotonic mapping are one reading (D61).
        session_clock = SessionClock()
        try:
            capture.start(
                dict(
                    type="start",
                    version=1,
                    captureId=capture_id,
                    timelineOriginMs=session_clock.origin_ms,
                    microphone=microphone,
                    title=meeting["title"],
                    callGeneration=capture_generation,
                ),
                others_source="system",
                reason="missing",
                refusals=refusals,
                session_clock=session_clock,
            )
        except Exception as error:
            capture.stop(str(error), user_stopped=False)
            with self.lock:
                if capture.directory is None:
                    self.captures.pop(capture_id, None)
                    self.call_captures = {
                        key: value for key, value in self.call_captures.items() if value[0] != capture_id
                    }
                if browser.states.get(tab, {}).get("captureId") == capture_id:
                    browser.states[tab] = dict(recording=False, tabId=tab)
            raise
        if cancelled is not None and cancelled.is_set() or deadline is not None and self.deadline_clock() >= deadline:
            capture.stop(reason="user", user_stopped=False)
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
        deadline = self.deadline_clock() + self.batch_timeout
        # Starts still pending when this Stop-all cancels them. Taken in the same
        # lock as the cancel: a woken Start can unwind and drop its pending entry
        # before a later look, and its browser would never get the Stop.
        cancelled_starts = set()
        if directory is None:
            with self.lock:
                cancelled_starts.update(
                    self._meeting_id(b, p["tabId"])
                    for b in self.browsers.values()
                    for p in b.pending.values()
                    if p.get("action") == "start"
                )
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
            # R93-2 (#230): Stop is marked before anything waits for a capture
            # lock (the snapshot below takes each one), which a stalled
            # checkpoint can hold: its moment is the press, and PCM queued
            # behind the stall finds the boundary when it gets the lock.
            with self.lock:
                captures = [
                    (key, c)
                    for key, c in self.captures.items()
                    if not c.finished and (directory is None or c.directory is not None and matches(str(c.directory)))
                ]
                for _key, capture in captures:
                    if quitting_pid is None:
                        self._remember_capture_stop(capture)
                    capture.request_stop(reason=reason, user_stopped=quitting_pid is None and reason == "user")
            snapshot = self.snapshot()
            paths = [
                s["path"]
                for s in snapshot["statuses"]
                if (s.get("recording") or s.get("stopping")) and matches(s.get("path"))
            ]
            with self.lock:
                commands = {s["id"] for s in snapshot["statuses"] if s.get("recording") and matches(s.get("path"))}
                if directory is None:
                    commands.update(cancelled_starts)
                    commands.update(
                        self._meeting_id(b, p["tabId"])
                        for b in self.browsers.values()
                        for p in b.pending.values()
                        if p.get("action") == "start"
                    )
            # Save native-owned audio before waiting for any browser acknowledgement.
            jobs = [
                (
                    "capture:" + key,
                    lambda capture=capture: capture.stop(
                        reason=reason, user_stopped=quitting_pid is None and reason == "user"
                    ),
                )
                for key, capture in captures
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

    def tick(self):
        """Close transports past their deadline, then run lifecycle policy.

        Runs during Stop too: policy pauses then, but a stuck peer must not.
        A closed transport unwinds through its reader's normal EOF path.
        """
        now = self.clock()
        with self.lock:
            transports = [*self.browsers.values(), *self._audio_transports]
        for transport in transports:
            # Never waits for a capture lock: this is the accept loop, and a
            # Stop connecting meanwhile must be read when it is pressed (#230).
            reason = transport.expired(now, wait=False)
            if reason is not None and not transport.shut_down.is_set():
                logging.getLogger(__name__).warning("Closing a browser peer that %s", reason)
                transport.shutdown()
        with self.lock:
            # Loss is checked first. Pings are unconditional, not idle-only, and
            # only queue: no socket I/O under the inventory lock.
            for browser in self.browsers.values():
                if (
                    browser.last_ping is not None
                    and not browser.shut_down.is_set()
                    and now - browser.last_ping >= HEARTBEAT_INTERVAL
                ):
                    browser.last_ping = now
                    ping = dict(type="ping", seq=browser.ping_seq)
                    browser.ping_seq += 1
                    with contextlib.suppress(OSError):
                        browser.send(ping)  # An overflow already shut the peer down.
        self.policy()

    def policy(self):
        if self.closed.is_set() or self._stopping_all.is_set():
            return
        tasks = []
        capture_tasks, cancels = [], []
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
                # Read once, never waiting for capture.lock, which a checkpoint
                # stalled on storage can hold for seconds: this runs under the
                # broker's lock, on its accept loop or a control reader, and a
                # Stop pressed meanwhile must reach the broker and be marked when
                # it is pressed (#230). A busy capture is decided on a later tick.
                readiness = capture.readiness(wait=False)
                if readiness is None:
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
                            and not readiness.since(lifecycle.missing)
                        ):
                            count = 0  # Returning pre-join slots are not received audio.
                    if page == "closed":
                        capture.tab_closed = True
                    if reconnect is not None:
                        known_page = peer is not None and peer[2] is not None
                        # A disconnect can be observed between two PCM buffers. Recent
                        # pre-disconnect PCM is still fresh; requiring the next
                        # buffer here prematurely invents a page-loss timer.
                        fresh = readiness.since()
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
                    current = source_state(capture.host.session.manifest.get("events"))
                    resume = None
                    if peer is not None:
                        attached = capture.audio_connected or any(t.capture is capture for t in self._audio_transports)
                        resume = ResumeInputs(
                            # D52.5 C1-C3, C7-C9; page, count and control are
                            # checked in the lifecycle's present-row branch.
                            # R82 1a: the D58 #2 stand-in (system/missing with the
                            # browser lost) is adopted the same way, D49's ask once,
                            # with loss 0: a later system/lost gets its own ask.
                            ready=RESUME_CAPTURE in peer[0].capabilities
                            and resumable(current, capture)
                            and not attached
                            and capture.call_generation is not None
                            and meeting.get("callGeneration") == capture.call_generation
                            # The extension's teardown of the old session must
                            # finish first, or it refuses the only ask.
                            and peer[1] not in peer[0].reported
                            # A decision that cannot be queued must not spend it.
                            and key not in self._capture_policy_pending,
                            loss=capture.loss_serial,
                            connection=peer[0].id,
                        )
                    action = lifecycle.decide(
                        now,
                        page,
                        count,
                        fallback=capture.browser_lost,
                        others=capture.others,
                        restore_ready=readiness.ready and not capture.restore_blocked,
                        fallback_pending=capture.fallback_pending,
                        multistream=capture.multistream_capable,
                        # D52.5: another call's heard never arms this recording's
                        # restore or OTHERS_GONE grace; it latches only on rows
                        # of the recording's own callGeneration, when it has one.
                        heard=meeting.get("heard") is True
                        and capture.call_generation in (None, meeting.get("callGeneration")),
                        inventory_known=peer is not None
                        and peer[2] is not None
                        and meeting.get("sourceCountKnown", True),
                        control_alive=peer is not None,
                        pcm_live=capture.multistream_capable and readiness.since(recorded=True),
                        initial_missing=current.reason == "missing",
                        resume=resume,
                    )
                    if reconnect is not None and page == "present" and count > 0 and lifecycle.missing is None:
                        self._control_reconnects.pop(key, None)
                    identifier = f"{peer[0].id}:{peer[1]}" if peer is not None else None
                    if action == "restore" and capture.fallback_pending:
                        # Cancelled on this thread below, never queued: the
                        # fallback may hold a worker while its device opens,
                        # and a full pool would run a queued cancel too late.
                        cancels.append((key, capture))
                    elif action and key not in self._capture_policy_pending:
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
                        and readiness.since(reconnect.since)
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
                            # A capture the user stopped, only saving, does not block
                            # a start the policy is otherwise allowed (#237).
                            and not self._generation_recording(tab, meeting, active=True)
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
        for key, capture in cancels:
            self._cancel_fallback(key, capture)
        for key, capture, identifier, action in capture_tasks:
            self.workers.submit(self._capture_lifecycle, key, capture, identifier, action)

    def _cancel_fallback(self, key, capture):
        """D52: the call returned before its fallback committed; write nothing.

        Sets the capture's cancel flag under the lock its commit checks, on the
        policy thread. A fallback that committed first stays (heard rule).
        The policy thread can be the accept loop: while a stalled checkpoint
        holds that lock, a thread of its own waits for it instead (never the
        worker pool, see policy()), returned so it can be joined (#230).
        """
        if capture.cancel_fallback(wait=False) is not None:
            return None
        waiter = threading.Thread(target=capture.cancel_fallback, name="cassini-fallback-cancel", daemon=True)
        waiter.start()
        return waiter

    def _capture_lifecycle(self, key, capture, identifier, action):
        try:
            if action == "fallback":
                if identifier is not None:
                    self._fallback(identifier, capture)
                else:
                    capture.fallback()
            elif action in ("restore", "switch:browser:callConnected"):
                capture.restore()
            elif action == "askResume":
                # Never a Stop: the catch-all below treats unknown actions so.
                self._ask_resume(key, capture, identifier)
            else:
                # Save before waiting for a potentially unreachable extension.
                reason = action.partition(":")[2] or "user"
                capture.stop(reason=reason, user_stopped=False)
                if identifier is not None:
                    self.command(identifier, "stop", reason=reason, expected_capture=key)
        except (OSError, ValueError, TimeoutError):
            logging.getLogger(__name__).exception("Browser capture lifecycle: %s", action)
        finally:
            with self.lock:
                self._capture_policy_pending.discard(key)

    def _resume_still_eligible(self, key, capture, browser, tab):
        """Every D52.5 ask condition, read again at send time (caller holds self.lock).

        The decision was taken on an earlier tick; the page may have navigated,
        the row's count become unknown or zero, the call changed, the extension
        reported the tab again, the loss ended or the connection gone since.
        """
        lifecycle = self._capture_lifecycles.get(key)
        state = browser.states.get(tab, {})
        meeting = browser.meetings.get(tab)
        if lifecycle is None or meeting is None:
            return False
        page = browser.page_states.get(tab) or state.get("pageState") or meeting.get("pageState")
        current = source_state(capture.host.session.manifest.get("events"))
        return bool(
            self.browsers.get(browser.id) is browser  # the same control connection, still open
            and not browser.closed
            and not browser.shut_down.is_set()
            and browser.status_seen
            and RESUME_CAPTURE in browser.capabilities
            and state.get("recording")
            and state.get("captureId") == key
            and page == "present"
            and meeting.get("sourceCountKnown", True) is True
            and meeting.get("sourceCount", 0) > 0
            and capture.call_generation is not None
            and meeting.get("callGeneration") == capture.call_generation
            and lifecycle.heard
            and tab not in browser.reported  # the extension released the tab
            and resumable(current, capture)
            and capture.browser_lost
            and capture.others
            and not (capture.finished or capture.stopping or capture.stop_requested.is_set())
            and not capture.audio_connected
            and not any(t.capture is capture for t in self._audio_transports)
            and lifecycle.asked_loss == capture.loss_serial  # the loss the ask was decided for
            and browser.id in lifecycle.asked_on
            and not self._stopping_all.is_set()
        )

    def _ask_resume(self, key, capture, identifier):
        """D52.5: send one resume-capture-v1 Start naming this lost recording.

        Not through command(): a recording tab is refused there, a rejection
        would become a fresh Start fallback (forbidden after a failed resume),
        a stale result would cancel the very target, and it would wait up to
        the command timeout. Nothing here retries.

        The ask was decided on an earlier tick. It is revalidated and queued
        under the inventory lock that guards the decision; when it no longer
        holds, nothing is sent and the reservation is released, so this loss
        and connection can still ask once it is eligible again. Queueing is
        not socket I/O: one writer thread owns the stream.
        """
        browser_id, _, raw_tab = (identifier or "").partition(":")
        with self.lock:
            browser = self.browsers.get(browser_id)
            if (
                browser is None
                or not raw_tab.isdecimal()
                or not self._resume_still_eligible(key, capture, browser, int(raw_tab))
            ):
                lifecycle = self._capture_lifecycles.get(key)
                if lifecycle is not None:
                    lifecycle.asked_on.discard(browser_id)
                return
            request_id = str(uuid4())
            packet = dict(
                type="command",
                requestId=request_id,
                action="start",
                mode="tracks",
                tabId=int(raw_tab),
                independent=True,
                captureLocal=False,
                callGeneration=capture.call_generation,
                resumeCapture=dict(captureId=key, timelineOriginMs=capture.origin_ms),
            )
            if "page-state-v1" in browser.capabilities:
                # As for any GNOME Start: a reload must not end the resumed session.
                packet["keepOnNavigation"] = True
            browser.resumes[request_id] = key
            while len(browser.resumes) > RESUME_HISTORY:
                browser.resumes.pop(next(iter(browser.resumes)))
            # R82 1b: one refusal entry per ask, from whichever answer comes first.
            capture.mark_resume_ask(request_id)
            try:
                browser.send(packet)
            except (OSError, ValueError) as error:
                browser.resumes.pop(request_id, None)
                capture.clear_resume_ask(request_id)
                logging.getLogger(__name__).info("Could not ask the browser to resume recording %s: %s", key, error)

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
                    meeting = browser.meetings.get(int(raw_tab), {}) if browser is not None else {}
                    if browser is None or (
                        "call-evidence-v1" in browser.capabilities
                        and not self._auto_start_ready(browser, meeting)
                        # The call may have gained a capture while this task waited.
                        or self._generation_recording(int(raw_tab), meeting, active=True)
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
        except StartRejected as rejected:
            if rejected.refusal.get("detail") == "busy":
                return  # D58 #11: it may be this tab already recording; never record it twice.
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
                        logical_identifier,
                        meeting,
                        automatic_generation=generation,
                        call_version=call_version,
                        refusals=[rejected.refusal],
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
                    browser = BrowserConnection(
                        stream,
                        packet.get("browserName"),
                        packet.get("browserInstance"),
                        connection=connection,
                        clock=self.clock,
                    )
                    with self.lock:
                        self.browsers[browser.id] = browser
                    try:
                        while (packet := read_packet(stream)) is not None:
                            self.update(browser, packet)
                    finally:
                        # Release a writer still blocked on this peer before
                        # closing the stream it writes to.
                        browser.shutdown()
                elif packet.get("role") == "application":
                    self._application(connection, stream, packet.get("active", True), packet.get("generation", 0))
                elif packet.get("role") == "meter":
                    from .spectro_transport import serve_meter

                    serve_meter(self, connection, stream, packet.get("path"))
                elif packet.get("role") == "audio":
                    self._audio(stream, connection)
                elif packet.get("role") == "app":
                    try:
                        action = packet.get("action")
                        if action == "status":
                            result = self.snapshot()
                        elif action == "start-all":
                            result = self.start_all(
                                microphone=packet.get("microphone", True),
                                group_id=packet.get("groupId"),
                                only=packet.get("only"),
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
                            result = capture.select(
                                packet.get("me"), packet.get("others"), retry=packet.get("retry") is True
                            )
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
        owner = self.captures.get(capture_id)
        if owner is not None and (owner.finished or capture_id in self._cancelled_captures):
            return
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
                        # call-end, return and transport deadlines must still advance.
                        self.tick()
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
                                capture.stop("Recording service closed", user_stopped=False)
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
