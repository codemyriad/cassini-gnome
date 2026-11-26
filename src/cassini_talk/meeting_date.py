# SPDX-License-Identifier: GPL-3.0-or-later
"""D112 (L-87) Set date and time: the dialog's prefill, the stored value and the worker edit.

The date set is stored as meeting.json `startedAt` (RFC 3339 with the person's
UTC offset, seconds floored) with `startedAtSource: "user"`. It wins over every
other start (LIBRARY-RULES R6.2) and is projected into the Cassini file's DATE,
manifest meeting.startedAt and recordedAtLocal (MAPPING.md). It never renames
the file. No gi imports: the dialog lives in set_date_dialog.py.
"""

import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import library_location as location
from .library import LibraryScanner, meeting_id
from .library_edits import transaction
from .library_schema import SCHEMA
from .library_schema import valid as schema_valid
from .meeting_store import read_metadata, update_metadata
from .meeting_time import local

EARLIEST_SAVED_MS = 946_684_800_000  # 2000-01-01: no recording file Cassini meets was saved earlier.


@dataclass(frozen=True)
class Prefill:
    """What the dialog opens with: a local wall clock to the minute, and whether it came from the file."""

    at: datetime
    from_file: bool = False


def _zone(tz):
    return ZoneInfo(tz) if isinstance(tz, str) else tz


def saved_ms(meeting):
    """The meeting audio's last-saved time (ms), or None. IO: call off the GTK thread when it can block."""
    paths = [meeting.audio_file or meeting.published_file] if (meeting.audio_file or meeting.published_file) else []
    if not paths:
        paths = [track.path for track in meeting.tracks if getattr(track, "path", None) is not None]
    times = []
    for path in paths:
        with suppress(OSError, TypeError, ValueError):
            times.append(os.stat(path).st_mtime_ns // 1_000_000)
    return max(times, default=None)


def prefill(meeting, *, tz, now_ms, saved=None):
    """The recording time if known; else the file's last-saved time minus the length; else now.

    Only the middle case shows "Filled in from when the file was saved…".
    """
    zone = _zone(tz)
    if meeting.started_at_ms is not None:
        at, from_file = datetime.fromtimestamp(meeting.started_at_ms / 1000, zone), False
    elif local(meeting.recorded_at_local) is not None:
        at, from_file = local(meeting.recorded_at_local), False
    elif saved is not None and EARLIEST_SAVED_MS <= saved <= now_ms + 86_400_000:
        # A zeroed or future mtime (copied fixtures, wrong clocks) says nothing; fall back to now.
        start = saved - (meeting.duration_ms or 0)
        at, from_file = datetime.fromtimestamp(start / 1000, zone), True
    else:
        at, from_file = datetime.fromtimestamp(now_ms / 1000, zone), False
    return Prefill(at.replace(second=0, microsecond=0, tzinfo=None), from_file)


def parse_clock_part(raw, upper):
    """Parse pending text from an hour/minute field; raises ValueError unless it is an integer in 0..upper."""
    cleaned = raw.strip()
    if not cleaned or not (cleaned.isascii() and cleaned.isdigit()):
        raise ValueError(f"Invalid time component: {raw!r}")
    value = int(cleaned)
    if not (0 <= value <= upper):
        raise ValueError(f"Time component out of range 0..{upper}: {value}")
    return value


def started_at(year, month, day, hour, minute, *, tz):
    """RFC 3339 for that local wall clock in the person's zone, with that day's UTC offset."""
    value = datetime(year, month, day, hour, minute, tzinfo=_zone(tz)).isoformat(timespec="seconds")
    if not schema_valid(value, SCHEMA["properties"]["startedAt"]):
        raise ValueError("The date and time cannot be stored with this time zone")
    return value


def changes(meeting, value):
    """Whether saving *value* changes the meeting: a different instant, or a first date set by the person."""
    return value != meeting.started_at_user


def set_started_at(directory, value):
    """Worker only: store *value* as the meeting's start (journaled for published files, never a rename)."""
    if not schema_valid(value, SCHEMA["properties"]["startedAt"]):
        raise ValueError("startedAt must be RFC 3339 with an explicit UTC offset")
    with location.routing(), transaction(directory):
        directory = location.owned_directory(Path(directory))
        if location.capture_busy(meeting_id(directory)):
            raise ValueError("The meeting is still recording or saving")
        meeting = next(
            (
                m
                for m in LibraryScanner.for_meeting(directory).scan(changed_directories={directory}).meetings
                if m.directory == directory
            ),
            None,
        )
        if meeting is None or meeting.lifecycle != "saved" or not meeting.metadata_editable:
            raise ValueError("Set date and time requires an editable saved Library meeting")
        current = read_metadata(directory)
        if current.get("startedAt") == value and current.get("startedAtSource") == "user":
            return current
        return update_metadata(directory, startedAt=value, startedAtSource="user")
