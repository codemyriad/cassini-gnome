# SPDX-License-Identifier: GPL-3.0-or-later
"""D35 saved-meeting titles; worker-only owned metadata transactions."""

from pathlib import Path

from . import library_location as location
from .library import LibraryScanner, meeting_id
from .library_edits import transaction
from .meeting_store import read_metadata, update_metadata


def title_value(value):
    if not isinstance(value, str):
        raise ValueError("A meeting title must be text")
    value = value.strip()
    if len(value) > 500 or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("A meeting title supports at most 500 Unicode characters")
    # D38.4: a blank name removes the local override.
    return value or None


def renames(meeting, metadata, title):
    """Whether saving *title* (a title_value result) changes the meeting.

    A blank rename (None) only changes a meeting whose metadata holds a user
    title or a non-automatic source. Missing meeting.json reads as automatic,
    like read_metadata, so it never enables a no-op save (D38.4).
    """
    if title is not None:
        return title != meeting.title
    return metadata.get("title") is not None or metadata.get("titleSource", "meeting-window") != "meeting-window"


def rename_meeting(directory, value):
    title = title_value(value)
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
            raise ValueError("Rename requires an editable saved Library meeting")
        current = read_metadata(directory)
        if not renames(meeting, current, title):
            return current
        return update_metadata(directory, title=title, titleSource="user" if title is not None else "meeting-window")
