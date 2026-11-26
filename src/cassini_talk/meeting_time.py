# SPDX-License-Identifier: GPL-3.0-or-later
"""D95: meeting time is distinct from the document's creation/seal time."""

import math
import re
from datetime import UTC, datetime

ABSOLUTE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
LOCAL = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\Z")


def absolute(value):
    if not isinstance(value, str) or not ABSOLUTE.fullmatch(value) or value.endswith("-00:00"):
        return None
    if not value.endswith("Z") and int(value[-2:]) >= 60:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def local(value):
    if not isinstance(value, str) or not LOCAL.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def utc(value):
    instant = absolute(value)
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z") if instant else None


def milliseconds(value):
    instant = absolute(value)
    return math.floor(instant.timestamp() * 1000) if instant else None


def user_start(metadata):
    """D112 (L-87): the start the person set (meeting.json / x meeting `startedAt`), as stored."""
    if not isinstance(metadata, dict) or metadata.get("startedAtSource") != "user":
        return None
    value = metadata.get("startedAt")
    return value if absolute(value) is not None else None


def import_time(metadata):
    """S-12 Q16: the name prefix is the import time, so the file carries no usable start."""
    return isinstance(metadata, dict) and metadata.get("namePrefixSource") == "import-time"


def ours(manifest):
    """The file's own x["codemyriad.io"] object, or {}."""
    extension = manifest.get("x") if isinstance(manifest, dict) else None
    value = extension.get("codemyriad.io") if isinstance(extension, dict) else None
    return value if isinstance(value, dict) else {}


def excluded(metadata):
    """LIBRARY-RULES R6.2 (D112): a legacy import-time file's embedded dates are never its start."""
    return import_time(metadata) and user_start(metadata.get("meeting")) is None and user_start(metadata) is None


def wall_clock(value):
    """MAPPING.md: recordedAtLocal is startedAt's local wall clock, without offset or fraction."""
    instant = absolute(value)
    return instant.replace(microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S") if instant else None


def selected(manifest, tags):
    if not manifest and any(key.upper() == "CASSINI_FORMAT" for key in tags):
        return None
    meeting = manifest.get("meeting", {}) if manifest else {}
    private = ours(manifest)
    value = user_start(private.get("meeting"))
    if value is not None:
        return dict(source="startedAt", value=value)
    if excluded(private):
        return None
    value = meeting.get("recordedAtUtc")
    if isinstance(value, str) and value.endswith("Z") and absolute(value):
        return dict(source="recordedAtUtc", value=value)
    value = meeting.get("startedAt")
    if absolute(value) is not None:
        return dict(source="startedAt", value=value)
    value = tags.get("DATE")
    instant = absolute(value)
    if instant is not None and instant != absolute(meeting.get("createdAtUtc")):
        return dict(source="DATE", value=value)
    value = meeting.get("recordedAtLocal")
    if local(value) is not None:
        return dict(source="recordedAtLocal", value=value)
    return None
