# SPDX-License-Identifier: GPL-3.0-or-later
"""D103 (L-58) Preferences map helpers without GTK, so the minimal test environment covers them."""

import os
from pathlib import Path

from .strings import text


def on_with_size(byte_count):
    """L-58: an installed speaker model reads "On · 62 MB" beside Remove (size from the manifest)."""
    from .ux_formats import size

    return text("models.statusWithSize", status=text("models.status.on"), size=size(byte_count))


def library_usage(root, cancelled=None):
    """Bytes the Library's meetings use: the root files and folders, plus each v2 working folder
    in `.cassini/sessions/` (recorded tracks live there, even after the `.opus` is published).

    The rest of the hidden state is not counted: `.cassini/trash` (deleted meetings),
    `.cassini/staging` and `.cassini/journal`. Symlinks are never followed, and a symlinked
    `.cassini` or `sessions` is skipped, as the scanner does. File IO: never call it on the GTK
    thread (bug class #2)."""
    root = Path(root)
    total = 0
    # (folder, skip its dot-entries): the root's own hidden state and `.cassini-moved-*` leftovers
    # in sessions are not meetings; hidden files inside a meeting folder still count.
    pending = [(root, True)]
    sessions = root / ".cassini" / "sessions"
    try:
        if not (root / ".cassini").is_symlink() and not sessions.is_symlink() and sessions.is_dir():
            pending.append((sessions, True))
    except OSError:
        pass
    while pending:
        if cancelled is not None and cancelled.is_set():
            break
        path, skip_hidden = pending.pop()
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if skip_hidden and entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((Path(entry.path), False))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return total
