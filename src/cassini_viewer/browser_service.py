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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

MAX_PACKET = 512 * 1024


def service_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    parent = Path(runtime) if runtime else Path("/tmp") / f"cassini-{os.getuid()}"
    return parent / "cassini" / "browser.sock"


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


def connect(path=None):
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(30)
    connection.connect(str(path or service_path()))
    return connection


def ensure_service(path=None):
    target = Path(path or service_path())
    try:
        connection = connect(target)
        connection.close()
        return target
    except OSError:
        pass
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
        try:
            connection = connect(target)
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
    def __init__(self, stream):
        self.id = str(uuid4())
        self.stream = stream
        self.write_lock = threading.Lock()
        self.meetings = {}
        self.status = {"recording": False}
        self.status_seen = False
        self.notification = None
        self.pending = {}
        self.attempted = set()
        self.error = None
        self.busy = False
        self.scheduled = False
        self.missing_since = None
        self.closed = False

    def send(self, packet):
        with self.write_lock:
            send_packet(self.stream, packet)


class BrowserService:
    def __init__(self, config=None, *, command_timeout=25, notifier=None):
        self.config = (
            Path(config)
            if config
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cassini/browser.json"
        )
        self.auto_record = False
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.auto_record = json.loads(self.config.read_text()).get("autoRecord") is True
        self.lock = threading.RLock()
        self.browsers = {}
        self.recordings = []
        self.command_timeout = command_timeout
        if notifier is None:
            from .browser_notifications import notify_recording

            notifier = notify_recording
        self.notifier = notifier
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cassini-browser")
        self.closed = threading.Event()

    def snapshot(self):
        with self.lock:
            meetings = []
            for browser in self.browsers.values():
                for meeting in browser.meetings.values():
                    meetings.append(
                        dict(
                            meeting,
                            id=f"{browser.id}:{meeting['tabId']}",
                            recording=browser.status.get("recording") is True
                            and browser.status.get("tabId") == meeting["tabId"],
                            busy=browser.busy,
                            browserRecording=browser.status.get("recording") is True,
                            error=browser.error,
                        )
                    )
            for meeting in meetings:
                if meeting["recording"]:
                    browser = self.browsers[meeting["id"].partition(":")[0]]
                    meeting["path"] = browser.status.get("path")
            return dict(
                browsers=len(self.browsers),
                autoRecord=self.auto_record,
                meetings=meetings,
                recordings=list(self.recordings),
                statuses=[
                    dict(
                        b.status,
                        **({"id": f"{b.id}:{b.status['tabId']}"} if type(b.status.get("tabId")) is int else {}),
                    )
                    for b in self.browsers.values()
                ],
            )

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
            if enabled:
                for browser in self.browsers.values():
                    browser.attempted.clear()
        self.policy()
        return self.snapshot()

    def _status(self, browser, value):
        status = {key: value[key] for key in ("recording", "tabId", "path", "message", "tracks") if key in value}
        if type(status.get("recording")) is not bool:
            return
        browser.status_seen = True
        previous = browser.status
        browser.status = status
        if not status["recording"] and browser.notification:
            self.workers.submit(self._close_notification, browser.notification)
            browser.notification = None
        if previous.get("recording") and not status["recording"] and isinstance(status.get("path"), str):
            recording = dict(
                path=status["path"],
                title=browser.meetings.get(previous.get("tabId"), {}).get("title", "Recorded meeting"),
                message=str(status.get("message", "Recording saved"))[:500],
            )
            self.recordings = [recording] + [r for r in self.recordings if r["path"] != recording["path"]][:19]

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
            elif kind != "hello":
                raise ValueError("Unknown browser control event")
        self.policy()

    def command(self, identifier, action):
        if action not in ("start", "stop") or not isinstance(identifier, str):
            raise ValueError("Invalid meeting command")
        browser_id, _, raw_tab = identifier.partition(":")
        with self.lock:
            browser = self.browsers.get(browser_id)
            if browser is None or not raw_tab.isdecimal():
                raise ValueError("This browser is no longer connected")
            tab = int(raw_tab)
            if action == "start" and tab not in browser.meetings:
                raise ValueError("This meeting is no longer available")
            if browser.busy:
                raise ValueError("A recording command is still being processed")
            if action == "stop" and (not browser.status.get("recording") or browser.status.get("tabId") != tab):
                raise ValueError("This meeting is not recording")
            browser.busy = True
            browser.error = None
            browser.attempted.add(tab)
            request_id = str(uuid4())
            pending = dict(event=threading.Event(), result=None)
            browser.pending[request_id] = pending
        try:
            browser.send(dict(type="command", requestId=request_id, action=action, tabId=tab, mode="tracks"))
            if not pending["event"].wait(self.command_timeout):
                raise TimeoutError("The browser did not finish the recording command")
            result = pending["result"]
            if result is None or result.get("ok") is not True:
                raise ValueError(str((result or {}).get("error", "The browser disconnected"))[:500])
            return self.snapshot()
        except (OSError, ValueError, TimeoutError) as error:
            with self.lock:
                browser.error = str(error)
            raise
        finally:
            with self.lock:
                browser.pending.pop(request_id, None)
                browser.busy = False

    def policy(self):
        tasks = []
        with self.lock:
            for browser in self.browsers.values():
                if browser.busy or browser.scheduled or browser.closed or not browser.status_seen:
                    continue
                if browser.status.get("recording"):
                    tab = browser.status.get("tabId")
                    if browser.meetings.get(tab, {}).get("sourceCount", 0) > 0:
                        browser.missing_since = None
                    elif browser.missing_since is None:
                        browser.missing_since = time.monotonic()
                    elif time.monotonic() - browser.missing_since >= 2:
                        browser.scheduled = True
                        tasks.append((f"{browser.id}:{tab}", "stop"))
                        browser.missing_since = float("inf")
                elif self.auto_record:
                    for tab in browser.meetings:
                        if tab not in browser.attempted and browser.meetings[tab]["sourceCount"] > 0:
                            browser.attempted.add(tab)
                            browser.scheduled = True
                            tasks.append((f"{browser.id}:{tab}", "start"))
                            break
        for identifier, action in tasks:
            self.workers.submit(self._automatic, identifier, action)

    def _automatic(self, identifier, action):
        try:
            self.command(identifier, action)
            if action == "start":
                with self.lock:
                    browser_id, _, tab = identifier.partition(":")
                    browser = self.browsers.get(browser_id)
                    if browser is None or not browser.status.get("recording"):
                        return
                    title = browser.meetings.get(int(tab), {}).get("title", "Browser meeting")
                try:
                    close = self.notifier(title, lambda: self.workers.submit(self._automatic, identifier, "stop"))
                    with self.lock:
                        if browser.status.get("recording"):
                            browser.notification = close
                        elif close:
                            self.workers.submit(self._close_notification, close)
                except Exception as error:
                    logging.getLogger(__name__).warning("Could not show recording notification: %s", error)
        except (OSError, ValueError, TimeoutError):
            pass  # The error is exposed in snapshots and never retried in a loop.
        finally:
            with self.lock:
                browser_id = identifier.partition(":")[0]
                if browser_id in self.browsers:
                    self.browsers[browser_id].scheduled = False

    @staticmethod
    def _close_notification(close):
        try:
            close()
        except Exception as error:
            logging.getLogger(__name__).warning("Could not close recording notification: %s", error)

    def handle(self, connection):
        browser = None
        try:
            with connection, connection.makefile("rwb") as stream:
                packet = read_packet(stream)
                if not packet:
                    return
                if packet.get("role") == "extension":
                    browser = BrowserConnection(stream)
                    with self.lock:
                        self.browsers[browser.id] = browser
                    while (packet := read_packet(stream)) is not None:
                        self.update(browser, packet)
                elif packet.get("role") == "app":
                    try:
                        action = packet.get("action")
                        if action == "status":
                            result = self.snapshot()
                        elif action == "configure":
                            result = self.configure(packet.get("autoRecord"))
                        else:
                            result = self.command(packet.get("id"), action)
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
                    self.workers.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", required=True)
    args = parser.parse_args()
    BrowserService().serve(args.serve)


if __name__ == "__main__":
    main()
