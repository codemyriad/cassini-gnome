# SPDX-License-Identifier: GPL-3.0-or-later
"""D42 single-flight build checks; no installed-file IO on GTK."""

import logging
import threading
from concurrent.futures import Future

LOG = logging.getLogger(__name__)


def recording_busy(app):
    from .capture_guard import capture_targets

    return bool(
        any(capture_targets(app))
        or getattr(app, "_browser_stops", ())
        or getattr(app, "_source_changes", ())
        or getattr(app, "_auto_recording_pending", None)
        or getattr(app, "_capture_background", False)
        or getattr(app, "_capture_background_saving", False)
        or getattr(app, "_capture_confirmation", None)
        or (getattr(app, "transcriber", None) is not None and app.transcriber.preparing())
    )


class BuildRefresh:
    def __init__(self, app, installed, *, post=None, later=None, cancel=None):
        if post is None:
            from gi.repository import GLib

            post, later, cancel = GLib.idle_add, GLib.timeout_add, GLib.source_remove
        self.app, self.installed = app, installed
        self.post, self.later, self.cancel = post, later, cancel
        self.pending = self.restarting = self.closed = self.held = False
        self.future = None
        self.revision = 0
        self.browser_busy = self.flushing = False
        self._activity_busy = None
        self.timer = 0
        self.callbacks = []
        self.show = False
        self.record = False
        self.files = []
        self.attempts = 0
        self._last_error = None
        self._banner_signature = None

    def check(self, callback=None, *, show=False, files=(), record=False):
        if self.closed:
            return
        self.revision += 1
        self.show |= show
        self.record |= record
        self.files.extend(uri for uri in files if uri not in self.files)
        if callback:
            self.callbacks.append(callback)
        if self.restarting:
            restart = self.installed.restart
            restart["show"] |= show
            restart["files"] = tuple(dict.fromkeys((*restart["files"], *files)))
            if record:
                restart["record"] = True
            return
        if self.future is not None or self.flushing:
            return
        if self.timer:
            self.cancel(self.timer)
            self.timer = 0
        if not self.held:
            self.app.hold()
            self.held = True
        revision = self.revision
        future = self.future = Future()
        future.revision = revision
        future.add_done_callback(lambda result: self.post(self._finished, result))

        def run():
            try:
                changed = self.installed.changed()
                # Consult the live broker as well as the GTK snapshot. The
                # broker/recording hosts live independently of GTK; a restart
                # never sends Stop to them.
                browser = False
                if changed and self.app.browser_watch is not None:
                    from . import browser_service

                    value = browser_service.request()
                    browser = any(s.get("recording") for s in value.get("statuses", ()))
                future.set_result((changed, browser))
            except Exception as error:
                future.set_exception(error)

        threading.Thread(target=run, name="cassini-installed-build", daemon=True).start()

    def _finished(self, future):
        self.future = None
        if self.closed:
            return False
        # A capture/activation transition during IO invalidates its snapshot.
        # Keep the accumulated intent and inspect the current state again.
        if future.revision != self.revision:
            self.check()
            return False
        changed = browser = False
        try:
            changed, browser = future.result()
        except Exception as error:
            if str(error) != self._last_error:
                LOG.info("Installed-build check deferred: %s", error)
                self._last_error = str(error)
            # An install may have published its manifest before its files. A
            # short bounded retry avoids importing the half-updated tree.
            self.attempts += 1
            if self.attempts <= 20:
                self.timer = self.later(500, self._retry)
                return False
            self.attempts = 0
            self._last_error = None
            self._continue_current()
            return False
        else:
            self.attempts = 0
            self._last_error = None
            self.pending = changed
            self.browser_busy = browser
        self._notify()
        if self.pending and not browser and not recording_busy(self.app):
            trash = getattr(self.app, "meeting_trash", None)
            if trash is not None:
                self.flushing = True
                if trash.flush(self._flushed, failed=self._flush_failed):
                    self._notify()
                    return False
                self.flushing = False
            self.restarting = True
            self.installed.restart = dict(
                show=self.show or any(w.get_visible() for w in self.app.get_windows()),
                automatic=self.app._auto_recording_held,
                files=tuple(self.files),
                writing=bool(self.app._transcription_holds),
            )
            if self.record:
                self.installed.restart["record"] = True
            self._notify()
            self._release()
            # Trash has already drained. No async gap is allowed between the
            # final safety check and closing the queue/process.
            self.app.finish_quit(trash_flushed=True)
            return False
        self._continue_current()
        return False

    def _flushed(self):
        if self.closed:
            return
        self.flushing = False
        # Capture may have started while the native Trash operation ran.
        self.check()

    def _flush_failed(self):
        self.flushing = self.restarting = False
        self.installed.restart = None
        self._notify()
        self._continue_current()

    def _continue_current(self):
        callbacks, self.callbacks = self.callbacks, []
        self.show = self.record = False
        self.files = []
        try:
            for callback in callbacks:
                callback()
        finally:
            self._release()

    def _retry(self):
        self.timer = 0
        self.check()
        return False

    def capture_changed(self):
        busy = recording_busy(self.app)
        changed = busy != self._activity_busy
        self._activity_busy = busy
        self._notify()
        if changed and self.future is not None:
            self.revision += 1
        if self.pending and not self.restarting and not self.flushing and not busy and (changed or self.future is None):
            self.check()

    @property
    def can_restart(self):
        return self.pending and not (self.restarting or self.flushing or self.browser_busy or recording_busy(self.app))

    def _notify(self):
        signature = self.pending, self.can_restart
        if signature == self._banner_signature:
            return
        self._banner_signature = signature
        for window in self.app.get_windows():
            if hasattr(window, "update_build_banner"):
                window.update_build_banner()

    def _release(self):
        if self.held and (self.future is None or self.closed):
            self.held = False
            self.app.release()

    def close(self):
        self.closed = True
        if self.timer:
            self.cancel(self.timer)
            self.timer = 0
        self.callbacks.clear()
        self._release()
