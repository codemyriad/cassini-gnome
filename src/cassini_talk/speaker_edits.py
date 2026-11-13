# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned-Library speaker edits. Call on a worker; publish one metadata revision."""

import unicodedata
from html import escape
from itertools import groupby
from pathlib import Path
from types import SimpleNamespace

from .library_edits import mutation as transaction
from .library_location import owned_directory
from .meeting_store import read_metadata, update_metadata
from .strings import text
from .transcript import display_items


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


# One 3-line ellipsized quote never shows more than ~250 characters, so format
# a bounded prefix of the chosen run instead of shaping a whole monologue.
QUOTE_WORDS = 80
QUOTE_CHARS = 500


def _quote_line(owner):
    words = getattr(owner, "words", ())
    display = getattr(owner, "display", None)
    if display is not None and len(display) == len(words):
        items = display[:QUOTE_WORDS]
    else:
        items = display_items(words[:QUOTE_WORDS])
    return " ".join(value for value in items if value).strip()[:QUOTE_CHARS]


def _pick_quote(candidates, sample):
    """Prefer a run overlapping *sample*, then the first run with visible text."""
    if sample is not None:
        start, end = sample
        for owner in candidates:
            if owner.end_ms > start and owner.start_ms < end and (line := _quote_line(owner)):
                return line
    for owner in candidates:
        if line := _quote_line(owner):
            return line
    return ""


def speaker_quote(meeting, identifier, sample=None):
    """A short line *identifier* said, linear in the transcript and bounded in length."""
    resolve = getattr(meeting, "speaker_for", None)
    resolved = {}

    def owns(raw):
        if resolve is None:
            return raw == identifier
        if raw not in resolved:
            resolved[raw] = resolve(raw).id
        return resolved[raw] == identifier

    candidates = [turn for turn in getattr(meeting, "listening_turns", ()) if owns(turn.speaker)]
    if line := _pick_quote(candidates, sample):
        return line
    # Reading rows keep short overlapping replies (chips) in row.asides.
    owners = []
    for row in getattr(meeting, "turns", ()):
        owners.append(row)
        owners.extend(aside for _after, aside in getattr(row, "asides", ()))
    candidates = sorted((owner for owner in owners if owns(owner.speaker)), key=lambda owner: owner.start_ms)
    if line := _pick_quote(candidates, sample):
        return line
    runs = []
    for speaker, group in groupby(getattr(meeting, "words", ()), key=lambda word: word.speaker):
        if owns(speaker):
            group = tuple(group)
            runs.append(SimpleNamespace(words=group, start_ms=group[0].start_ms, end_ms=max(w.end_ms for w in group)))
    return _pick_quote(runs, sample)


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
