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


def selected(manifest, tags):
    if not manifest and any(key.upper() == "CASSINI_FORMAT" for key in tags):
        return None
    meeting = manifest.get("meeting", {}) if manifest else {}
    value = meeting.get("recordedAtUtc")
    if isinstance(value, str) and value.endswith("Z") and absolute(value):
        return dict(source="recordedAtUtc", value=value)
    value = tags.get("DATE")
    instant = absolute(value)
    if instant is not None and instant != absolute(meeting.get("createdAtUtc")):
        return dict(source="DATE", value=value)
    value = meeting.get("recordedAtLocal")
    if local(value) is not None:
        return dict(source="recordedAtLocal", value=value)
    return None
