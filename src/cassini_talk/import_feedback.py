# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 shared catalog projection; no IO, widgets or worker state."""

import errno

from . import import_types
from .i18n import join_list
from .strings import plural, text


def accepted_formats():
    """L-25 {formats}: the decided contract labels as a locale list (Opus/Ogg, MP3, M4A/AAC, WAV and FLAC)."""
    return join_list(import_types.labels())


def cant_import_detail():
    return text("import.drop.cantImport.detail", formats=accepted_formats())


def droppable_name(name):
    """A dropped file name the importer takes: a decided type (never .ogv/.oga) or a ZIP."""
    return name.lower().endswith(".zip") or import_types.acceptable_name(name)


def storage_full(error):
    """BUG-CLASSES #15: a full disk (or quota) anywhere in the raised chain."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT):
            return True
        error = error.__cause__ or error.__context__
    return False


def failure_reason(error):
    """Batch reason code for a failed import: storage-full has its own copy."""
    return "diskFull" if storage_full(error) else "damaged"


def single_failure(error):
    """Toast for one failed import; never the raw OS/decoder text (BUG-CLASSES #13)."""
    if storage_full(error):
        return text("import.failed.diskFull")
    return plural("import.summary.failed", 1)


def progress(result):
    total = max(1, result.total)
    completed = result.imported + result.duplicates + result.ignored + result.failed
    return text("import.progress", done=min(completed + 1, total), total=total)


def summary(result):
    parts = [
        plural("import.summary." + name, count)
        for name, count in (
            ("imported", result.imported),
            ("duplicates", result.duplicates),
            ("unsupported", result.ignored),
            ("failed", result.failed),
        )
        if count
    ]
    if any(reason == "diskFull" for _name, reason in result.errors):
        # Its own line with the recovery step, apart from the failed count.
        parts.append(text("import.failed.diskFull"))
    if result.cancelled:
        parts.append(text("import.cancelled"))
    reasons = {
        "damaged": "damaged",
        "encrypted": "encrypted",
        "unsafe": "unsafePaths",
        "limit": "tooLarge",
        "empty": "empty",
    }
    for name, reason in result.errors:
        if name.lower().endswith(".zip") and reason in reasons:
            parts.append(name + ": " + text("import.zip.failed." + reasons[reason]))
            break
    return " · ".join(parts) if parts else text("import.zip.failed.empty")
