# SPDX-License-Identifier: GPL-3.0-or-later
"""Read files outside the Library without importing or modifying them (UX4 later)."""

from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from . import portable
from .library import LibraryScanner, Meeting, _date, _opus, _speakers, library_root, turn_gaps
from .meeting_text import reading_turns, speaker_turns
from .portable_context import embedded_notes, empty_word_transcripts


def load_open_meeting(path, root=None, *, scanner=None, capture_activity=None):
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
        reader = scanner if owned and scanner is not None else LibraryScanner.for_meeting(directory)
        snapshot = reader.scan(capture_activity=capture_activity if owned else None, changed_directories={directory})
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
    extensions = embedded.manifest.get("x") if embedded.manifest else None
    extensions = extensions if isinstance(extensions, dict) else {}
    gaps = turn_gaps((), {}, extensions)
    return Meeting(
        id=str(uuid5(NAMESPACE_URL, path.as_uri())),
        directory=path.parent,
        title=embedded.title or path.stem,
        started_at_ms=embedded.recorded_at_ms,
        recorded_at_local=embedded.recorded_at_local,
        created_at_ms=_date(embedded.created_at),
        duration_ms=duration,
        origin="received",
        transcript=state,
        embedded_transcript=state == "done" and bool(words) and embedded.is_readable,
        audio_file=path,
        audio_size_bytes=audio_size,
        has_audio=duration is not None,
        words=words,
        turns=reading_turns(words, gaps=gaps),
        listening_turns=speaker_turns(words),
        turn_gaps=gaps,
        speakers=_speakers(words, [], embedded, stored_labels=True),
        metadata_editable=False,
        notes=embedded_notes(embedded),
        extensions=extensions,
    )
