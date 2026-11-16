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
    """Bytes in the Library folder, without its hidden state (`.cassini`, which holds the Trash).

    File IO: never call it on the GTK thread (bug class #2)."""
    total = 0
    pending = [Path(root)]
    top = True
    while pending:
        if cancelled is not None and cancelled.is_set():
            break
        path = pending.pop()
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if top and entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            pass
        top = False
    return total
