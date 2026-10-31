# SPDX-License-Identifier: GPL-3.0-or-later
"""Read files outside the Library without importing or modifying them (UX4 later)."""

from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from . import portable
from .library import LibraryScanner, Meeting, _date, _opus, _speakers, library_root
from .meeting_text import reading_turns
from .portable_context import embedded_notes, empty_word_transcripts


def load_open_meeting(path, root=None):
    path = Path(path).resolve()
    directory = path if path.is_dir() else path.parent
    root = library_root(root).resolve()
    from .library_location import owned_directory

    try:
        owned = (
            owned_directory(directory) == directory if root == library_root().resolve() else directory.parent == root
        )
    except ValueError:
        owned = False
    if path.name == "session.json" or path.is_dir() or owned and directory.name.startswith("Received-"):
        snapshot = LibraryScanner.for_meeting(directory).scan(changed_directories={directory})
        meeting = next((m for m in snapshot.meetings if m.directory == directory), None)
        if meeting is not None and (path.is_dir() or path.name == "session.json" or meeting.audio_file == path):
            return replace(meeting, metadata_editable=meeting.metadata_editable and owned)
        if path.is_dir() or path.name == "session.json":
            raise ValueError("Recording folder cannot be read")
    with path.open("rb") as stream:
        embedded, duration, trusted, _, audio_size = _opus(stream, path.stat())
    try:
        words = (
            tuple(portable.load_words(embedded, embedded.default_transcript_id))
            if trusted and embedded.is_readable
            else ()
        )
    except ValueError:
        words = ()
    state = "damaged"
    if (
        embedded.state == portable.State.PLAIN_AUDIO
        or trusted
        and embedded.is_readable
        and empty_word_transcripts(embedded)
    ):
        state = "none"
    elif trusted and embedded.is_readable:
        try:
            portable.load_words(embedded, embedded.default_transcript_id)
            state = "done"
        except ValueError:
            pass
    return Meeting(
        id=str(uuid5(NAMESPACE_URL, path.as_uri())),
        directory=path.parent,
        title=embedded.title or path.stem,
        started_at_ms=_date(embedded.created_at),
        duration_ms=duration,
        origin="received",
        transcript=state,
        audio_file=path,
        audio_size_bytes=audio_size,
        has_audio=duration is not None,
        words=words,
        turns=reading_turns(sorted(words, key=lambda w: w.start_ms)),
        speakers=_speakers(words, [], embedded, stored_labels=True),
        metadata_editable=False,
        notes=embedded_notes(embedded),
        extensions=embedded.manifest.get("x", {})
        if embedded.manifest and isinstance(embedded.manifest.get("x"), dict)
        else {},
    )
