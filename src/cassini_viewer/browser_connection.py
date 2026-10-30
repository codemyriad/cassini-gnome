# SPDX-License-Identifier: GPL-3.0-or-later
"""D31 quiet single-flight setup, shared retry policy for native and Flatpak."""

import errno
import logging
import os
import random
import threading
from concurrent.futures import Future

from . import companion
from .strings import text

LOG = logging.getLogger(__name__)


def state_text(state):
    if state is None:
        return text("settings.browser.status.notSetUp")
    if state["phase"] == "connected":
        return (
            text("browser.state.connected", browser=", ".join(state["names"]))
            if state["names"]
            else text("browser.state.connectedGeneric")
        )
    if state["phase"] == "failed":
        return text("browser.state.failed", reason=text("browser.state.reason." + state["reason"]))
    return text("browser.state." + state["phase"])


def add_text(state):
    from .companion_bundle import store_listing_public

    if not store_listing_public():
        return text("companion.local.add")
    browser = state.get("browser") if state else None
    return text("browser.state.add", browser=browser.name) if browser else text("browser.state.addGeneric")


class Backoff:
    def __init__(self, jitter=random.uniform):
        self.jitter = jitter
        self.reset()

    def reset(self):
        self.base = 5

    def next(self):
        delay = min(300, self.base * (1 + self.jitter(-0.2, 0.2)))
        self.base = min(300, self.base * 2)
        return delay


def setup():
    ready, retryable = companion.probe()
    if not ready:
        raise companion.SetupError("temporary" if retryable else "unsafe", retryable=retryable, available=False)
    from .companion_bundle import prepare

    try:
        prepare()
    except (OSError, ValueError):
        # Folder preparation cannot invalidate an existing store connection.
        # An explicit Add from Cassini retries it and shows its own true error.
        LOG.warning("Could not prepare the local browser companion", exc_info=True)
    browsers = companion.installed_browsers()
    if not browsers:
        raise companion.SetupError("noBrowser")
    try:
        companion.register(browsers=browsers)
    except OSError as error:
        error.browsers = tuple(browsers)
        raise
    return tuple(browsers)


def classify(error):
    if isinstance(error, companion.SetupError):
        return error
    if isinstance(error, OSError):
        if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return companion.SetupError("unwritable")
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            return companion.SetupError("unsafe")
    return companion.SetupError("temporary", retryable=True)


class BrowserConnection:
    def __init__(
        self,
        changed,
        *,
        available=True,
        attempt=setup,
        later=None,
        post=None,
        cancel=None,
        jitter=random.uniform,
        ever_connected=False,
    ):
        if later is None:
            from gi.repository import GLib

            def later(seconds, callback):
                return GLib.timeout_add(max(1, round(seconds * 1000)), callback, priority=GLib.PRIORITY_LOW)

            def post(callback, *args):
                return GLib.idle_add(callback, *args, priority=GLib.PRIORITY_LOW)

            cancel = GLib.source_remove
        self.changed, self.attempt = changed, attempt
        self.later, self.post, self.cancel = later, post, cancel
        self.backoff = Backoff(jitter)
        self.available = available
        self.closed = self.succeeded = self.connected = False
        self.ever_connected = bool(ever_connected)
        self.error = None
        self.browsers = self.names = ()
        self.future = None
        self.timer = 0
        self.revision = 0

    def snapshot(self):
        phase = (
            "connected"
            if self.connected
            else "settingUp"
            if self.future is not None
            else "needsBrowser"
            if self.error and self.error.reason == "noBrowser"
            else "failed"
            if self.error
            else "waiting"
            if self.succeeded
            else "settingUp"
        )
        browser = self.browsers[0] if self.browsers else None
        return dict(
            phase=phase,
            names=self.names,
            browser=browser,
            reason=self.error.reason if self.error else None,
            everConnected=self.ever_connected,
        )

    def observe(self, snapshot):
        if self.closed:
            return
        before = self.connected, self.names
        self.connected = bool(snapshot.get("connectedBrowsers", snapshot.get("browsers")))
        self.ever_connected |= self.connected
        self.names = tuple(snapshot.get("browserNames", []))
        if before != (self.connected, self.names):
            self.changed()

    def trigger(self):
        if self.closed:
            return
        self.revision += 1
        self.backoff.reset()
        if self.timer:
            self.cancel(self.timer)
            self.timer = 0
        if self.future is None:
            self._start()

    def _start(self):
        if self.closed:
            return False
        self.timer = 0
        self.error = None
        future, revision = Future(), self.revision
        self.future = future
        future.add_done_callback(lambda f: self.post(self._finished, f, revision))

        def work():
            if not future.set_running_or_notify_cancel():
                return
            try:
                # Linux task priority applies to this worker, never GTK.
                tid = threading.get_native_id()
                os.setpriority(os.PRIO_PROCESS, tid, max(10, os.getpriority(os.PRIO_PROCESS, tid)))
                result = self.attempt()
            except Exception as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        # Quit must not wait for an external host command's timeout.
        threading.Thread(target=work, name="cassini-browser-setup", daemon=True).start()
        self.changed()
        return False

    def _finished(self, future, revision):
        if not future.done():
            return False
        self.future = None
        if self.closed:
            return False
        if revision != self.revision:
            self._start()  # A trigger during IO coalesces into one fresh check.
            return False
        try:
            self.browsers = future.result()
        except Exception as error:
            self.error = classify(error)
            if self.error.reason == "noBrowser":
                LOG.info("No supported browser is installed")
            else:
                LOG.warning("Background browser setup failed: %s", error)
            self.browsers = getattr(error, "browsers", self.browsers)
            if self.error.reason == "noBrowser":
                self.browsers = ()
            self.available = self.error.available
            if self.error.retryable:
                self.timer = self.later(self.backoff.next(), self._start)
        else:
            self.available = self.succeeded = True
            self.backoff.reset()
        self.changed()
        return False

    def close(self):
        self.closed = True
        if self.timer:
            self.cancel(self.timer)
            self.timer = 0
        if self.future:
            self.future.cancel()
