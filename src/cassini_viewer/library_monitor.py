# SPDX-License-Identifier: GPL-3.0-or-later
"""Incremental worker scans, trailing debounce and main-context snapshot delivery."""

import contextlib
import logging
import os
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

    def _watch_roots(self):
        root = self.scanner.root.absolute()
        roots = {root}
        targets = {self._ancestor(root), self._ancestor(root.parent)}
        resolved = root
        with contextlib.suppress(OSError, RuntimeError):
            resolved = root.resolve()
            roots.add(resolved)
            targets.update((self._ancestor(resolved), self._ancestor(resolved.parent)))
        return roots, targets, resolved

    def _add_watches(self, paths, roots, existing, created):
        # Called on the scan worker before reads. Deliver events explicitly on
        # the main context, even if GIO chooses another context for the monitor.
        added = set()
        for path in paths - existing.keys() - created.keys():
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                continue

            def changed(watch, file, other, event, active_roots=frozenset(roots), directory=path):
                GLib.idle_add(self._changed, watch, file, other, event, active_roots, frozenset((directory,)))

            monitor.connect("changed", changed)
            created[path] = monitor
            added.add(path)
        return added

    def _scan_and_watch_paths(self, activity, changed, existing, directories):
        roots, targets = self._roots, set(existing)
        created, late = {}, set()
        try:
            roots, targets, resolved = self._watch_roots()
            self._add_watches(targets, roots, existing, created)
            # Install child watches before the scanner can read those children.
            # Root watches already cover children created during this discovery.
            if changed is None:
                directories = set()
                try:
                    with os.scandir(resolved) as entries:
                        directories = {
                            Path(entry.path)
                            for entry in entries
                            if meeting_id(Path(entry.name)) and entry.is_dir(follow_symlinks=False)
                        }
                except OSError:
                    pass
            self._add_watches(directories, roots, existing, created)
            snapshot = self.scanner.scan(capture_activity=activity, changed_directories=changed)
            new_roots, new_targets, _resolved = self._watch_roots()
            roots = new_roots
            # If an absent root appeared during bootstrap, verify its contents
            # after its new root watch is attached. The usual path needs no rescan.
            late_root = self._add_watches(new_targets, roots, existing, created)
            late = None if late_root else self._add_watches(set(snapshot.directories), roots, existing, created)
            targets |= new_targets | set(snapshot.directories)
        except Exception:
            LOG.exception("Library worker scan failed")
            snapshot = LibrarySnapshot(
                diagnostics=(Diagnostic(self.scanner.root, "library-scan-failed"),),
                unavailable=True,
                directories=tuple(directories),
            )
            targets |= set(directories)
        return snapshot, roots, targets, created, late

    def _sync_watches(self, snapshot, roots, targets, created):
        self._roots = roots
        self._directories = set(snapshot.directories)
        self._monitors.update(created)
        for path in self._monitors.keys() - targets:
            self._monitors.pop(path).cancel()

    def _changed(self, _monitor, file, other_file, event, roots=None, directories=None):
        if self._closed:
            return
        roots = self._roots if roots is None else roots
        directories = self._directories if directories is None else directories
        changed = set()
        for value in (file, other_file):
            name = value.get_path() if value else None
            if name is None:
                continue
            path = Path(name)
            if any(path == r or path in r.parents or path.parent == r and meeting_id(path) for r in roots):
                self.refresh()
                return
            if path.parent in directories and (
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

    def refresh_now(self):
        """Controller phase changes bypass metadata debounce; IO stays on the worker."""
        self.refresh()
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._dispatch()

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
        self._future = self._executor.submit(
            self._scan_and_watch_paths, activity, changed, dict(self._monitors), set(self._directories)
        )
        self._future.add_done_callback(lambda future: GLib.idle_add(self._complete, future))

    def _complete(self, future):
        self._future = None
        try:
            snapshot, roots, targets, created, late = future.result()
        except Exception:
            if self._closed:
                return GLib.SOURCE_REMOVE
            LOG.exception("Library watch preparation failed")
            snapshot = LibrarySnapshot(
                diagnostics=(Diagnostic(self.scanner.root, "library-scan-failed"),), unavailable=True
            )
            roots, targets, created, late = self._roots, set(self._monitors), {}, set()
        if self._closed:
            for monitor in created.values():
                monitor.cancel()
            return GLib.SOURCE_REMOVE
        self._sync_watches(snapshot, roots, targets, created)
        if late is None or late:
            self.refresh(late)
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
