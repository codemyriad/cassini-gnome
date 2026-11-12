# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 shared catalog projection; no IO, widgets or worker state."""

from .strings import plural, text


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
