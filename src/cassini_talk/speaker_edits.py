# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned-Library speaker edits. Call on a worker; publish one metadata revision."""

import unicodedata
from html import escape
from itertools import groupby
from pathlib import Path
from types import SimpleNamespace

from .i18n import join_list
from .library_edits import mutation as transaction
from .library_location import owned_directory
from .meeting_store import read_metadata, update_metadata
from .strings import plural, text
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
        _apply_name(entry, value)
        return update_metadata(meeting.directory, speakers=list(speakers.values()))


# D112 (L-86): the members one Save in Name the speakers may change, and Undo puts back.
SPEAKER_KEYS = ("name", "provenance", "nameCleared", "background")


def _apply_name(entry, value):
    entry.update(name=value, provenance="user" if value is not None else "unnamed")
    if value is None:
        entry["nameCleared"] = True
    else:
        entry.pop("nameCleared", None)


def save_speakers(directory, changes, *, scanner=None):
    """Apply every pending speaker change from one Save in one metadata revision.

    *changes* maps a visible speaker id to ``{"name": str}`` and/or
    ``{"background": bool}``. A background voice keeps its stored name, so
    switching it off restores it; ``background`` is written only as ``true``
    and removed when switched off (meeting.schema.json, D112 / L-86).
    Returns ``(metadata, previous)``: *previous* maps each changed id to its
    earlier ``SPEAKER_KEYS`` members, or None when it had no stored entry, for
    ``restore_speakers`` (Undo).
    """
    values = {}
    for identifier, change in changes.items():
        values[identifier] = dict(change)
        if "name" in change:
            values[identifier]["name"] = name_value(change["name"])
    directory = owned_directory(Path(directory))
    with transaction(directory):
        meeting = current_meeting(directory, scanner=scanner)
        visible = {s.id: s for s in meeting.speakers}
        if not values or any(identifier not in visible for identifier in values):
            raise ValueError("Speaker is no longer visible")
        metadata = read_metadata(meeting.directory)
        speakers = {s["id"]: dict(s) for s in metadata["speakers"]}
        previous = {}
        for identifier, change in values.items():
            old = speakers.get(identifier)
            previous[identifier] = None if old is None else {k: old[k] for k in SPEAKER_KEYS if k in old}
            stored = visible[identifier].stored_colour_index
            colour_index = stored if stored is not None else visible[identifier].colour_index
            entry = speakers.setdefault(
                identifier, dict(id=identifier, name=None, provenance="unnamed", colourIndex=colour_index)
            )
            if "name" in change:
                _apply_name(entry, change["name"])
            if change.get("background") is True:
                entry["background"] = True
            elif "background" in change:
                entry.pop("background", None)
        return update_metadata(meeting.directory, speakers=list(speakers.values())), previous


def restore_speakers(directory, previous, *, scanner=None):
    """Undo one ``save_speakers``: put back each speaker's earlier name and background members."""
    directory = owned_directory(Path(directory))
    with transaction(directory):
        meeting = current_meeting(directory, scanner=scanner)
        metadata = read_metadata(meeting.directory)
        speakers = {s["id"]: dict(s) for s in metadata["speakers"]}
        for identifier, members in previous.items():
            entry = speakers.get(identifier)
            if entry is None:
                continue
            for key in SPEAKER_KEYS:
                entry.pop(key, None)
            if members is None:
                entry.update(name=None, provenance="unnamed")
            else:
                entry.update(members)
                entry.setdefault("name", None)
                entry.setdefault("provenance", "unnamed")
        updated = list(speakers.values())
        if updated == metadata["speakers"]:
            return metadata
        return update_metadata(meeting.directory, speakers=updated)


def save_summary(changes, display_names):
    """D112 (L-86): the one toast for a Save, or None when no speaker became a background voice.

    *changes* holds ``(id, was_background, background, new_name_or_None)`` per changed speaker;
    *display_names* maps ids to their shown names ("Speaker 5").
    """
    backgrounds = [identifier for identifier, was, now, _name in changes if now and not was]
    named = [name.strip() for _id, _was, now, name in changes if not now and name and name.strip()]
    if backgrounds and named:
        return plural(
            "speakers.backgroundVoice.toast.namedAndBackground",
            len(backgrounds),
            name=join_list(named),
        )
    if len(backgrounds) == 1:
        return text("speakers.backgroundVoice.toast.single", name=display_names[backgrounds[0]])
    if backgrounds:
        return plural("speakers.backgroundVoice.toast.count", len(backgrounds))
    return None


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
