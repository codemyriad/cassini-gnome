# SPDX-License-Identifier: GPL-3.0-or-later
"""D15.1 worker lifecycle. GTK only observes status and submits explicit moves."""

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from gi.repository import GLib

from . import library_location as location
from .capture_guard import recorder_busy

LOG = logging.getLogger(__name__)


class LibraryRelocator:
    def __init__(self, app):
        self.app = app
        self.closed = False
        self.cancelled = Event()
        self.future = None
        self._moving = False
        self.state = None
        self._held = False
        self._error = None
        self._failed_destination = None
        self._next = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-library-move")
        self.timer = GLib.timeout_add_seconds(1, self.poll)
        self.poll()

    @property
    def running(self):
        return self._moving

    def poll(self):
        if self.closed:
            return False
        if self.future is None:
            self._start()
        return True

    def move(self, destination):
        self._start(destination=destination)

    def retry(self):
        self._start(destination=self._failed_destination, retry=self._failed_destination is None)

    def _start(self, destination=None, retry=False):
        if self.closed:
            return
        if self.future is not None:
            if destination is not None or retry:
                self._next = (destination, retry)
                self._moving = True
                self._render()
            return
        self._operation_destination = destination
        self._operation_folder = location.primary()
        self._operation_count = max(
            (len(w.snapshot.meetings) for w in self.app.get_windows() if getattr(w, "snapshot", None)), default=0
        )
        activity, busy = {}, set()
        recorder = self.app.recorder
        if recorder_busy(recorder) and recorder.session:
            busy.add(recorder.session.directory)
        transcriber = self.app.transcriber
        if transcriber:
            with transcriber.lock:
                busy.update(transcriber.pending)
                busy.update(getattr(transcriber, "voice_pending", ()))
        for window in self.app.get_windows():
            activity.update(getattr(window, "_activity", {}))
        editor = self.app.comfort.editor if self.app.comfort else None
        edits = editor.submit(lambda: None) if destination is not None and editor else None

        def work():
            if edits:
                edits.result()
            if destination is not None:
                location.request(destination)
                GLib.idle_add(self._changed)
            value = location.load()
            if value["pending"]:
                from .capture_activity import read_browser_activity

                activity.update(read_browser_activity())
                return location.process(
                    busy=busy,
                    activity=activity,
                    retry=retry,
                    cancelled=self.cancelled,
                    changed=lambda: GLib.idle_add(self._changed),
                    started=lambda: GLib.idle_add(self._began_move),
                )
            return value

        if destination is not None or retry or (self.state and self.state["pending"]):
            self.app.hold()
            self._held = True
        self._moving = destination is not None or retry
        self.future = self.executor.submit(work)
        self.future.add_done_callback(lambda f: GLib.idle_add(self._complete, f))
        self._render()

    def _complete(self, future):
        self.future = None
        was_moving = self._moving
        self._moving = False
        previous = self.state
        try:
            self.state = future.result()
            self._error = None
            self._failed_destination = None
        except (OSError, ValueError):
            LOG.exception("Library relocation failed")
            self._error = dict(folder=self._operation_folder, count=self._operation_count)
            self._failed_destination = self._operation_destination
        if self._held:
            self._held = False
            self.app.release()
        if not self.closed and self._next:
            (destination, retry), self._next = self._next, None
            self._start(destination=destination, retry=retry)
        elif not self.closed and (was_moving or self.state != previous or self._error):
            self._changed()
        return False

    def _began_move(self):
        if not self.closed:
            self._moving = True
            if not self._held:
                self.app.hold()
                self._held = True
            self._render()
        return False

    def _changed(self):
        if not self.closed:
            for window in self.app.get_windows():
                if hasattr(window, "monitor"):
                    window.monitor.refresh_now()
            self._render()
        return False

    def _render(self):
        dialog = self.app.settings_dialog
        if dialog and not dialog.closed:
            dialog.render_moves(self.state, moving=self.running, error=self._error)

    def close(self):
        self.closed = True
        self._next = None
        self.cancelled.set()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self._held:
            self._held = False
            self.app.release()
        self.executor.shutdown(wait=False, cancel_futures=True)
