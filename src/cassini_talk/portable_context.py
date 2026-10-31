# SPDX-License-Identifier: GPL-3.0-or-later
"""D12 empty word bodies and D13 producer-owned personal notes."""

from copy import deepcopy

from . import portable


def empty_word_transcripts(embedded, cache=None):
    if embedded is None or not embedded.is_readable:
        return False
    entries = [e for e in embedded.transcripts if e.format == portable.WORDS_FORMAT]
    if not entries:
        return False
    cache = {} if cache is None else cache
    for entry in entries:
        if entry.id not in cache:
            try:
                cache[entry.id] = tuple(portable.load_words(embedded, entry.id))
            except ValueError:
                cache[entry.id] = None
        if cache[entry.id] is None or cache[entry.id]:
            return False
    return True


def embedded_notes(embedded):
    if embedded.manifest is None:
        return ()
    extension = embedded.manifest.get("x")
    domain = extension.get("gocassini.com") if isinstance(extension, dict) else None
    notes = domain.get("notes") if isinstance(domain, dict) else None
    if not isinstance(notes, list):
        return ()
    return tuple(
        deepcopy(note)
        for note in notes
        if isinstance(note, dict)
        and type(note.get("atMs")) is int
        and 0 <= note["atMs"] <= 9007199254740991
        and isinstance(note.get("text"), str)
        and len(note["text"]) <= 100000
    )
