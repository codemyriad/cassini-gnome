# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned-Library speaker edits. Call on a worker; publish one metadata revision."""

import unicodedata
from html import escape
from pathlib import Path

from .library_edits import mutation as transaction
from .library_location import owned_directory
from .meeting_store import read_metadata, update_metadata
from .strings import text


def voice_initial(name):
    stripped = name.strip()
    return stripped[0].upper() if stripped else "?"


def duplicate_notice_markup(candidate_name):
    before, _, after = text("speakers.duplicate.notice", name="\x00").partition("\x00")
    return (
        f"{escape(before, quote=False)}"
        f'<span weight="600">{escape(candidate_name, quote=False)}</span>'
        f"{escape(after, quote=False)}"
    )


def speaker_quote(meeting, identifier, sample=None):
    resolve = getattr(meeting, "speaker_for", None)
    turns = [
        turn
        for turn in getattr(meeting, "listening_turns", ())
        if (resolve(turn.speaker).id if resolve else turn.speaker) == identifier and turn.text.strip()
    ]
    if sample is not None:
        start, end = sample
        for turn in turns:
            if turn.end_ms > start and turn.start_ms < end:
                return turn.text.strip()
    if turns:
        return turns[0].text.strip()
    for row in getattr(meeting, "turns", ()):
        row_speaker = resolve(row.speaker).id if resolve and hasattr(row, "speaker") else getattr(row, "speaker", None)
        if row_speaker == identifier:
            line = " ".join(w.text.strip() for w in getattr(row, "words", ()) if w.text.strip())
            if line:
                return line
    words = [
        w.text.strip()
        for w in getattr(meeting, "words", ())
        if (resolve(w.speaker).id if resolve else w.speaker) == identifier and w.text.strip()
    ]
    return " ".join(words)


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
        stored = visible[identifier].stored_colour_index
        colour_index = stored if stored is not None else visible[identifier].colour_index
        entry = speakers.setdefault(identifier, dict(id=identifier, colourIndex=colour_index))
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
