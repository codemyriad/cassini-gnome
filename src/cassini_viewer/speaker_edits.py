# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned-Library speaker edits. Call on a worker; publish one metadata revision."""

import unicodedata
from pathlib import Path

from .library_edits import transaction
from .library_location import owned_directory
from .meeting_store import read_metadata, update_metadata


def name_value(value):
    value = value.strip()
    if len(value) > 64 or any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
        raise ValueError("Speaker names need at most 64 characters and no control characters")
    return value or None


def current_meeting(directory, *, scanner=None):
    from .library import LibraryScanner

    directory = owned_directory(Path(directory))
    scanner = scanner if scanner is not None else LibraryScanner.for_meeting(directory)
    meeting = next(
        (m for m in scanner.scan(changed_directories={directory}).meetings if m.directory == directory), None
    )
    if meeting is None or not meeting.metadata_editable:
        raise ValueError("Speaker edits require an editable meeting in the Library")
    return meeting


def save_name(directory, identifier, value, *, scanner=None):
    value = name_value(value)
    directory = owned_directory(Path(directory))
    with transaction(directory):
        meeting = current_meeting(directory, scanner=scanner)
        visible = {s.id: s for s in meeting.speakers}
        if identifier not in visible:
            raise ValueError("Speaker is no longer visible")
        metadata = read_metadata(meeting.directory)
        speakers = {s["id"]: dict(s) for s in metadata["speakers"]}
        entry = speakers.setdefault(identifier, dict(id=identifier, colourIndex=visible[identifier].colour_index))
        entry.update(name=value, provenance="user" if value is not None else "unnamed")
        if value is None:
            entry["nameCleared"] = True
        else:
            entry.pop("nameCleared", None)
        return update_metadata(meeting.directory, speakers=list(speakers.values()))


def merge_into(directory, source, target, *, scanner=None):
    directory = owned_directory(Path(directory))
    with transaction(directory):
        meeting = current_meeting(directory, scanner=scanner)
        visible = {s.id for s in meeting.speakers}
        if source == target or source not in visible or target not in visible:
            raise ValueError("Choose two different visible speakers")
        metadata = read_metadata(meeting.directory)
        history = list(metadata.get("speakerMerges", []))
        history.append({"from": source, "into": target})
        return update_metadata(meeting.directory, speakerMerges=history)


def undo_merge(directory, *, scanner=None):
    directory = owned_directory(Path(directory))
    with transaction(directory):
        meeting = current_meeting(directory, scanner=scanner)
        metadata = read_metadata(meeting.directory)
        history = list(metadata.get("speakerMerges", []))
        if not history:
            return metadata
        return update_metadata(meeting.directory, speakerMerges=history[:-1])
