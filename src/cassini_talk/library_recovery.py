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

    def project(self, snapshot):
        from dataclasses import replace

        from .publication_state import project

        with self.lock:
            failures = dict(self.failures)
        meetings = tuple(project(meeting, failures.get(meeting.directory)) for meeting in snapshot.meetings)
        return replace(snapshot, meetings=meetings) if meetings != snapshot.meetings else snapshot

    def observe(self, snapshot):
        from .publication_state import session_root_path

        if self.cancelled.is_set() or self.future is not None or self.timer:
            return
        if any(
            meeting.lifecycle == "saved"
            and (
                meeting.capture_state == "completed"
                or (
                    meeting.capture_state == "interrupted"
                    and bool(meeting.capture_events and meeting.capture_events[-1].get("type") == "end")
                )
            )
            and any(track.frames > 0 for track in meeting.tracks)
            and meeting.published_file is None
            and not meeting.published_missing
            and session_root_path(meeting.directory) is not None
            and meeting.directory not in self.attempted
            for meeting in snapshot.meetings
        ):
            self.start()

    def report(self, directory, error):
        with self.lock:
            self.failures[directory] = error
            self.attempted.add(directory)
        self._changed()

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

        from .library_v2 import journal
        from .publication_state import session_root_path

        if self.cancelled.is_set():
            return False
        directory = Path(directory).absolute()
        identity = journal.identity_of(directory)
        if identity is None or identity[0] != "Recording" or session_root_path(directory) is None:
            raise ValueError("Publication retry is outside the configured Library")
        # This is only lexical validation. _sweep reads the configured roots and
        # recover_completed validates paths/symlinks on the worker before writes.
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

    def _retry_paths(self, roots):
        """Resolve queued dialog paths on this worker, including paired-Move aliases."""
        from pathlib import Path

        from .library_publication import root_for

        allowed = {Path(root).resolve() for root in roots}
        with self.lock:
            requests = [
                (path, revision) for path, revision in self._requested.items() if revision <= self.started_revision
            ]
        for requested, revision in requests:
            if self.cancelled.is_set():
                break
            try:
                directory = library_location.owned_directory(requested)
                if root_for(directory) not in allowed:
                    raise ValueError("Publication retry is outside the configured Library")
            except (OSError, ValueError) as error:
                with self.lock:
                    self.failures[requested] = error
                    self.attempted.add(requested)
                GLib.idle_add(self._changed)
                continue
            with self.lock:
                self.attempted.discard(directory)
                self.failures.pop(directory, None)
                self._requested[directory] = max(self._requested.get(directory, 0), revision)

    def _sweep(self):
        skipped = []
        roots = library_location.roots()
        self._retry_paths(roots)
        for root in roots:
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
        # Keep newer requests accepted while this sweep was in flight. Consume
        # older ones so a later scan cannot clear an attempted failure again.
        with self.lock:
            self._requested = {
                path: revision for path, revision in self._requested.items() if revision > self.started_revision
            }
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
