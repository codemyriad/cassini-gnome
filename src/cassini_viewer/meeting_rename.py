# SPDX-License-Identifier: GPL-3.0-or-later
"""D35 saved-meeting titles; worker-only owned metadata transactions."""

from pathlib import Path

from . import library_location as location
from .library import LibraryScanner, meeting_id
from .meeting_store import LOCK, read_metadata, update_metadata


def title_value(value):
    if not isinstance(value, str):
        raise ValueError("A meeting title must be text")
    value = value.strip()
    if len(value) > 500 or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("A meeting title supports at most 500 Unicode characters")
    # D38.4: a blank name removes the local override.
    return value or None


def rename_meeting(directory, value):
    title = title_value(value)
    with location.routing(), LOCK:
        directory = location.owned_directory(Path(directory))
        if location.capture_busy(meeting_id(directory)):
            raise ValueError("The meeting is still recording or saving")
        meeting = next(
            (
                m
                for m in LibraryScanner(directory.parent).scan(changed_directories={directory}).meetings
                if m.directory == directory
            ),
            None,
        )
        if meeting is None or meeting.lifecycle != "saved" or not meeting.metadata_editable:
            raise ValueError("Rename requires an editable saved Library meeting")
        current = read_metadata(directory)
        source = "user" if title is not None else "meeting-window"
        if (title is not None and title == meeting.title) or (
            title is None and current.get("title") is None and current.get("titleSource") == source
        ):
            return current
        return update_metadata(directory, title=title, titleSource=source)
