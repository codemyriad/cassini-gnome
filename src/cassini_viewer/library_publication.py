# SPDX-License-Identifier: GPL-3.0-or-later
"""App-side Stop/recovery orchestration for the shared S-12 publisher.

All entry points do IO and belong on a worker. This module owns no journal:
recovery, encoding and commit are delegated to library_v2. A capture's lease
must remain held from its first checkpoint through publication, so a second
process never finalizes its still-saving session.
"""

import os
from concurrent.futures import CancelledError
from dataclasses import dataclass
from pathlib import Path

from . import meeting_store
from .export_audio import check_cancel
from .library_v2 import journal
from .library_v2.fsutil import fsync_dir


def root_for(directory):
    """Recognise a hidden session without following links out of its Library."""
    directory = Path(directory).absolute()
    if journal.identity_of(directory) is None or directory.parent.name != "sessions":
        return None
    if directory.parent.parent.name != journal.META:
        return None
    if any(path.is_symlink() for path in (directory, directory.parent, directory.parent.parent)):
        raise ValueError("Unsafe Library session path")
    return directory.parent.parent.parent


def capture_parent(root):
    """Create and sync the managed layout before the first capture checkpoint."""
    root = Path(root).resolve()
    missing = []
    parent = root
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)
        fsync_dir(parent.parent)
    layout = journal.Layout(root)
    with journal.LibraryLock(layout):
        return layout.sessions


def publish_stopped(session, *, cancel=None):
    """Publish a stopped managed capture, keeping PCM on every failure.

    Explicit legacy/fixture parents are left alone. The producer calls this
    outside its PCM/transition locks after closing its writers and checkpointing
    the end event; it retains the capture lease until this function returns.
    """
    from .library_v2 import finalize

    if not session.finished:
        raise ValueError("Capture writers are still active")
    root = root_for(session.directory)
    if root is None:
        return None
    check_cancel(cancel)
    return finalize.finalize(root, session.manifest["id"], cancel=cancel)


@dataclass(frozen=True)
class RecoveryResult:
    directory: Path
    published: object = None
    error: Exception | None = None


def recover_completed(root, *, cancel=None):
    """Replay first, then publish idle completed sessions one at a time (§11).

    A failed candidate is reported once and its WAVs are kept. Published-but-
    missing files, live captures and legacy root folders are never synthesized
    again. The caller may stop between results; no unbounded PCM is retained.
    """
    from .library_location import capture_lease
    from .library_v2 import finalize

    root = Path(root).resolve()
    layout = journal.Layout(root)
    if not layout.meta.exists():
        return
    if layout.meta.is_symlink() or layout.sessions.is_symlink():
        raise ValueError("Unsafe Library metadata path")
    check_cancel(cancel)
    report = journal.recover(root)
    for directory in sorted(layout.sessions.iterdir()):
        check_cancel(cancel)
        identity = journal.identity_of(directory)
        if identity is None or identity[0] != "Recording" or directory.is_symlink() or not directory.is_dir():
            continue
        origin, identifier = identity
        if report.blocks(origin, identifier):
            continue
        try:
            lease = capture_lease(identifier)
        except BlockingIOError:
            continue  # Includes saving, not just active PCM capture.
        try:
            try:
                session = finalize.read_session(directory)
                if session.get("id") != identifier or session.get("state") != "completed":
                    continue
                with meeting_store.LOCK:
                    metadata = meeting_store.read_metadata(directory)
                if metadata.get("published") is True:
                    continue
                check_cancel(cancel)
                result = finalize.finalize(root, identifier, cancel=cancel)
            except CancelledError:
                raise
            except (OSError, ValueError, RuntimeError) as error:
                yield RecoveryResult(directory, error=error)
            else:
                yield RecoveryResult(directory, published=result)
        finally:
            os.close(lease)
