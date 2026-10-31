# SPDX-License-Identifier: GPL-3.0-or-later
"""One bounded startup sweep, observed by GTK without doing filesystem IO there."""

import logging
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

from gi.repository import GLib

from . import library_location
from .library_publication import recover_completed

LOG = logging.getLogger(__name__)


class LibraryRecovery:
    def __init__(self, app):
        self.app = app
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self._activity = {}
        self.failures = {}
        self.attempted = set()
        self.timer = 0
        self.future = None
        self.retry_revision = self.started_revision = 0
        self._requested = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-library-recover")
        self.start()

    @property
    def activity(self):
        with self.lock:
            return dict(self._activity)

    def _phase(self, directory, saving):
        identifier = directory.name.removeprefix("Recording-")
        with self.lock:
            if saving:
                self._activity[identifier] = "saving"
            else:
                self._activity.pop(identifier, None)
        GLib.idle_add(self._changed)

    def _changed(self):
        if not self.cancelled.is_set():
            self.app._transcription_changed()
        return False

    def start(self):
        self.timer = 0
        if self.cancelled.is_set() or self.future is not None:
            return False
        self.started_revision = self.retry_revision
        self.app.hold()
        self.future = self.executor.submit(self._sweep)
        self.future.add_done_callback(lambda future: GLib.idle_add(self._complete, future))
        return False

    def retry(self, directory):
        """GTK action: retry this retained session, including during an active sweep."""
        from pathlib import Path

        from .library_publication import root_for
        from .library_v2 import journal

        if self.cancelled.is_set():
            return False
        directory = library_location.owned_directory(Path(directory).absolute())
        identity = journal.identity_of(directory)
        if identity is None or identity[0] != "Recording" or root_for(directory) not in {Path(root).resolve() for root in library_location.roots()}:
            raise ValueError("Publication retry is outside the configured Library")
        with self.lock:
            self.attempted.discard(directory)
            self.failures.pop(directory, None)
            self.retry_revision += 1
            self._requested[directory] = self.retry_revision
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.future is None:
            self.start()
        return True

    def _sweep(self):
        skipped = []
        for root in library_location.roots():
            if self.cancelled.is_set():
                break
            try:
                for result in recover_completed(
                    root,
                    cancel=self.cancelled,
                    started=lambda path: self._phase(path, True),
                    finished=lambda path: self._phase(path, False),
                    skipped=lambda: skipped.append(True),
                    excluded=self.attempted,
                ):
                    if result.error:
                        LOG.warning("Library publication kept the original audio: %s", result.error)
                    with self.lock:
                        if self._requested.get(result.directory, 0) <= self.started_revision:
                            self.attempted.add(result.directory)
                        if result.error:
                            self.failures[result.directory] = result.error
                        else:
                            self.failures.pop(result.directory, None)
                    GLib.idle_add(self._changed)
            except CancelledError:
                break
            except (OSError, ValueError):
                LOG.exception("Library recovery could not access a Library; its contents are kept")
        return bool(skipped)

    def _complete(self, future):
        self.future = None
        try:
            needs_retry = future.result()
        except CancelledError:
            needs_retry = False
        except Exception:
            LOG.exception("Library recovery failed; original audio is kept")
            needs_retry = False
        self.app.release()
        if trash := getattr(self.app, "meeting_trash", None):
            trash.executor.submit(trash.cleanup.retry)
        if not self.cancelled.is_set():
            if self.retry_revision != self.started_revision:
                self.timer = GLib.idle_add(self.start)
            elif needs_retry:
                self.timer = GLib.timeout_add_seconds(5, self.start)
        return False

    def close(self):
        self.cancelled.set()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.executor.shutdown(wait=False, cancel_futures=True)
