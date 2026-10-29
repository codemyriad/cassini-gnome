# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable retry of private bookkeeping after the system Trash has succeeded."""

import json
import logging
import os
import stat
from pathlib import Path

from . import library_location as location
from .library import meeting_id
from .meeting_store import atomic_json

LOG = logging.getLogger(__name__)
LIMIT = 1024 * 1024


class TrashCleanup:
    def __init__(self, voices):
        self.voices = voices
        self.path = voices.root.parent / "trash-pending.json"

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Invalid Trash cleanup journal")
            raw = source.read(LIMIT + 1)
        value = json.loads(raw)
        if (
            len(raw) > LIMIT
            or not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("pending"), list)
        ):
            raise ValueError("Invalid Trash cleanup journal")
        paths = value["pending"]
        if any(not isinstance(p, str) or not Path(p).is_absolute() or not meeting_id(Path(p)) for p in paths):
            raise ValueError("Invalid Trash cleanup meeting")
        return paths

    def _save(self, paths):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(self.path, dict(version=1, pending=paths))

    def add(self, directory):
        paths = self._read()
        value = str(directory)
        if value not in paths:
            self._save(paths + [value])

    def forget(self, directory):
        paths = self._read()
        self._save([p for p in paths if p != str(directory)])

    def finish(self, directory):
        self.voices.remove_meeting(directory)
        location.forget_meeting(directory)
        self.forget(directory)

    def retry(self):
        try:
            for name in self._read():
                directory = Path(name)
                if not directory.parent.is_dir():
                    continue  # An unavailable drive is not evidence of removal.
                try:
                    current = location.owned_directory(directory)
                except ValueError:
                    current = directory
                if current.exists():
                    self.forget(directory)  # Aborted Trash or an already-restored meeting.
                else:
                    self.finish(directory)
        except (OSError, ValueError):
            LOG.exception("Private Trash cleanup remains queued for a later launch")
