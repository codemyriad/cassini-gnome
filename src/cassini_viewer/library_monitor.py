# SPDX-License-Identifier: GPL-3.0-or-later
"""GFileMonitor adapter; the scanner and model have no GLib dependency."""

import contextlib
from pathlib import Path

from gi.repository import Gio, GLib

from .library import LibraryScanner, meeting_id


class LibraryMonitor:
    def __init__(self, callback, root=None, *, capture_activity=None, debounce_ms=75):
        self.scanner = LibraryScanner(root)
        self.callback = callback
        self.capture_activity = capture_activity or (lambda: {})
        self.debounce_ms = debounce_ms
        self._monitors = []
        self._pending = 0
        self._closed = False
        self._watch()

    def _watch(self):
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors.clear()
        root = self.scanner.root
        ancestor = root
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        paths = [ancestor]
        if root.is_dir():
            # The scanner reports an unavailable root distinctly.
            with contextlib.suppress(OSError):
                paths += [p for p in root.iterdir() if meeting_id(p) and p.is_dir() and not p.is_symlink()]
        for path in paths:
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                # A disappearing/inaccessible directory must not prevent the scan
                # from reporting its unavailable state. Explicit refresh retries.
                continue
            monitor.connect("changed", self._changed)
            self._monitors.append(monitor)

    def _changed(self, _monitor, file, other_file, event):
        if self._closed:
            return
        relevant = False
        for changed in (file, other_file):
            value = changed.get_path() if changed else None
            if value is None:
                continue
            path = Path(value)
            if (
                path == self.scanner.root
                or path in self.scanner.root.parents
                or meeting_id(path)
                or path.name in ("session.json", "meeting.json", "transcript.json")
                or path.suffix == ".opus"
                or path.suffix == ".wav"
                and event != Gio.FileMonitorEvent.CHANGED
            ):
                relevant = True
        if relevant:
            self.refresh()

    def refresh(self):
        """Also called when the external activity adapter changes recording/saving."""
        if not self._closed and not self._pending:
            self._pending = GLib.timeout_add(self.debounce_ms, self._reload)

    def _reload(self):
        self._pending = 0
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._watch()
        self.callback(self.scanner.scan(capture_activity=self.capture_activity()))
        return GLib.SOURCE_REMOVE

    def close(self):
        self._closed = True
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors.clear()
