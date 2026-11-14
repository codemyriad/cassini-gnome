# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-device Library stars (L-34, D99, D53.1).

Stars are Library state on this device, never part of a Cassini file or an
export: they live in `<Library>/.cassini/stars.json`, which every scanner and
exporter already skips (S-12 §1). A star is keyed by the meeting's stable
Library key: the session folder name (`Recording-<uuid>` / `Received-<uuid>`)
for meetings Cassini made or imported, which a title rename and the session to
published `.opus` step both keep, or `file/<sha256>` for a standalone file.

Writes are read-modify-write under `.cassini/stars.lock` (flock), so two
processes never lose each other's stars, then replace the file atomically and
durably (temp file, fsync, rename, directory fsync). All disk work runs on one
worker thread; the caller's state updates at once and the worker's view is
adopted only when no newer change is still queued.
"""

import fcntl
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .library_v2 import fsutil
from .library_v2.journal import META

LOG = logging.getLogger(__name__)
FILE = "stars.json"
LOCK = "stars.lock"
VERSION = 1
MAX_BYTES = 1 << 20


def star_key(meeting):
    """The Library key a star follows; stable across rename and publication."""
    return meeting.key


def parse(data):
    """Starred keys from file bytes. Anything unreadable reads as no stars."""
    if len(data) > MAX_BYTES:
        return frozenset()
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return frozenset()
    if not isinstance(value, dict) or value.get("version") != VERSION:
        return frozenset()
    keys = value.get("starred")
    if not isinstance(keys, list):
        return frozenset()
    return frozenset(key for key in keys if isinstance(key, str) and key and len(key) <= 512)


def serialize(keys):
    return (json.dumps({"version": VERSION, "starred": sorted(keys)}, ensure_ascii=False, indent=1) + "\n").encode()


def read(root):
    try:
        return parse((Path(root) / META / FILE).read_bytes())
    except FileNotFoundError:
        return frozenset()
    except OSError:
        LOG.warning("Could not read stars in %s", root, exc_info=True)
        return frozenset()


def update(root, key, starred):
    """Durably add or remove one key, merging with whatever is on disk now."""
    return _change(root, add={key} if starred else (), remove=() if starred else {key})


def merge(root, keys):
    """Add *keys* (stars carried from a previous Library location) to *root*."""
    return _change(root, add=keys) if not frozenset(keys) <= read(root) else read(root)


def _change(root, *, add=(), remove=()):
    meta = Path(root) / META
    meta.mkdir(parents=True, exist_ok=True)
    fd = os.open(meta / LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = (read(root) | frozenset(add)) - frozenset(remove)
        fsutil.write_durable(meta / FILE, serialize(current))
        return current
    finally:
        os.close(fd)


class StarStore:
    """One Library root's stars. Call from the GTK thread; disk work is off it."""

    def __init__(self, root, *, dispatch=None, on_changed=None, inherit=frozenset()):
        self.root = Path(root)
        self._dispatch = dispatch or (lambda callback: callback())
        self._on_changed = on_changed
        self._starred = frozenset()
        self._issued = 0
        self.loaded = False
        self.closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-stars")
        # A moved Library keeps its meetings' keys, so its stars come along.
        self._starred = frozenset(inherit)
        inherited = frozenset(inherit)

        def load():
            if not inherited:
                return read(self.root)
            try:
                return merge(self.root, inherited)
            except OSError:
                LOG.warning("Could not carry stars to %s", self.root, exc_info=True)
                return read(self.root)

        self._submit(load, 0)

    def is_starred(self, key):
        return key in self._starred

    @property
    def starred(self):
        return self._starred

    def set(self, key, starred):
        starred = bool(starred)
        if self.closed or (key in self._starred) == starred:
            return
        self._starred = self._starred | {key} if starred else self._starred - {key}
        self._issued += 1
        root = self.root

        def work():
            try:
                return update(root, key, starred)
            except OSError:
                # Read-only, unmounted or full: show what is really stored.
                LOG.warning("Could not save the star for %s", key, exc_info=True)
                return read(root)

        self._submit(work, self._issued)
        self._changed()

    def toggle(self, key):
        self.set(key, not self.is_starred(key))
        return self.is_starred(key)

    def _submit(self, work, generation):
        future = self._executor.submit(work)
        future.add_done_callback(lambda done: self._dispatch(lambda: self._finished(done, generation)))
        return future

    def _finished(self, future, generation):
        if self.closed:
            return False
        try:
            stored = future.result()
        except Exception:
            LOG.exception("Star store update failed")
            return False
        if generation == 0:
            self.loaded = True
        if generation == self._issued and stored != self._starred:
            self._starred = stored
            self._changed()
        elif generation == 0 and self._issued == 0:
            self._changed()
        return False

    def _changed(self):
        if self._on_changed is not None:
            self._on_changed(self)

    def flush(self):
        """Wait for queued disk work (tests and shutdown)."""
        self._executor.submit(lambda: None).result()

    def close(self):
        # Queued writes still finish: executor workers are joined at exit.
        self.closed = True
        self._executor.shutdown(wait=False)
