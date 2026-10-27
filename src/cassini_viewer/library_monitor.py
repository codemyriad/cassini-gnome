# SPDX-License-Identifier: GPL-3.0-or-later
"""Incremental worker scans, trailing debounce and main-context snapshot delivery."""

import contextlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gi.repository import Gio, GLib

from .library import Diagnostic, LibraryScanner, LibrarySnapshot, meeting_id

LOG = logging.getLogger(__name__)


class LibraryMonitor:
    def __init__(self, callback, root=None, *, capture_activity=None, debounce_ms=250, max_wait_ms=2000):
        self.scanner = LibraryScanner(root)
        self.callback = callback
        self.capture_activity = capture_activity or (lambda: {})
        self.debounce_ms = debounce_ms
        self.max_wait_ms = max_wait_ms
        self._monitors = {}
        self._directories = set()
        self._timer = 0
        self._first_event = None
        self._pending_full = False
        self._pending_dirs = set()
        self._ready = False
        self._closed = False
        self._future = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-library")
        self._roots = {self.scanner.root.absolute()}

    @staticmethod
    def _ancestor(path):
        while not path.is_dir() and path != path.parent:
            path = path.parent
        return path

    def _scan_and_watch_paths(self, activity, changed):
        snapshot = self.scanner.scan(capture_activity=activity, changed_directories=changed)
        root = self.scanner.root.absolute()
        roots = {root}
        targets = {self._ancestor(root)} | set(snapshot.directories)
        with contextlib.suppress(OSError, RuntimeError):
            resolved = root.resolve()
            roots.add(resolved)
            targets.add(self._ancestor(resolved))
        if root.is_symlink():
            targets.add(root.parent)
        return snapshot, roots, targets

    def _sync_watches(self, snapshot, roots, targets):
        self._roots = roots
        self._directories = set(snapshot.directories)
        for path in self._monitors.keys() - targets:
            self._monitors.pop(path).cancel()
        for path in targets - self._monitors.keys():
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                continue
            monitor.connect("changed", self._changed)
            self._monitors[path] = monitor

    def _changed(self, _monitor, file, other_file, event):
        if self._closed:
            return
        roots = self._roots
        changed = set()
        for value in (file, other_file):
            name = value.get_path() if value else None
            if name is None:
                continue
            path = Path(name)
            if any(path == r or path in r.parents or path.parent == r and meeting_id(path) for r in roots):
                self.refresh()
                return
            if path.parent in self._directories and (
                path.name in ("session.json", "meeting.json", "transcript.json")
                or path.suffix == ".opus"
                and event != Gio.FileMonitorEvent.CHANGED
            ):
                changed.add(path.parent)
        if changed:
            self.refresh(changed)

    def refresh(self, directories=None):
        """Manual activity/root refresh, or a set of affected meeting directories."""
        if self._closed:
            return
        if directories is None:
            self._pending_full = True
        else:
            self._pending_dirs.update(directories)
        now = GLib.get_monotonic_time() // 1000
        if self._first_event is None:
            self._first_event = now
        if self._timer:
            GLib.source_remove(self._timer)
        remaining = max(0, self.max_wait_ms - (now - self._first_event))
        self._timer = GLib.timeout_add(min(self.debounce_ms, remaining), self._dispatch)

    def _dispatch(self):
        self._timer = 0
        if not self._closed:
            self._ready = True
            if self._future is None:
                self._start_scan()
        return GLib.SOURCE_REMOVE

    def _start_scan(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        changed = None if self._pending_full else set(self._pending_dirs)
        activity = dict(self.capture_activity())
        self._pending_full = False
        self._pending_dirs.clear()
        self._first_event = None
        self._ready = False
        self._future = self._executor.submit(self._scan_and_watch_paths, activity, changed)
        self._future.add_done_callback(lambda future: GLib.idle_add(self._complete, future))

    def _complete(self, future):
        self._future = None
        if self._closed:
            return GLib.SOURCE_REMOVE
        try:
            snapshot, roots, targets = future.result()
        except Exception:
            LOG.exception("Library worker scan failed")
            snapshot = LibrarySnapshot(
                diagnostics=(Diagnostic(self.scanner.root, "library-scan-failed"),), unavailable=True
            )
            roots, targets = self._roots, set(self._monitors)
        self._sync_watches(snapshot, roots, targets)
        self.callback(snapshot)
        if self._ready and not self._closed:
            self._start_scan()
        return GLib.SOURCE_REMOVE

    def close(self):
        self._closed = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        for monitor in self._monitors.values():
            monitor.cancel()
        self._monitors.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
