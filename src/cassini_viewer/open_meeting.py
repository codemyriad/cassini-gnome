# SPDX-License-Identifier: GPL-3.0-or-later
"""Read files outside the Library without importing or modifying them (UX4 later)."""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from . import portable
from .library import LibraryScanner, Meeting, _date, _opus, _speakers
from .meeting_text import reading_turns


def load_open_meeting(path):
    path = Path(path).resolve()
    directory = path if path.is_dir() else path.parent
    if path.name == "session.json" or path.is_dir():
        snapshot = LibraryScanner(directory.parent).scan()
        meeting = next((m for m in snapshot.meetings if m.directory == directory), None)
        if meeting is None:
            raise ValueError("Recording folder cannot be read")
        return meeting
    with path.open("rb") as stream:
        embedded, duration, trusted, _ = _opus(stream, path.stat())
    words = (
        tuple(portable.load_words(embedded, embedded.default_transcript_id)) if trusted and embedded.is_readable else ()
    )
    state = "done" if words else "none" if embedded.state == portable.State.PLAIN_AUDIO else "damaged"
    return Meeting(
        id=str(uuid5(NAMESPACE_URL, path.as_uri())),
        directory=path.parent,
        title=embedded.title or path.stem,
        started_at_ms=_date(embedded.created_at),
        duration_ms=duration,
        origin="received",
        transcript=state,
        audio_file=path,
        has_audio=duration is not None,
        words=words,
        turns=reading_turns(sorted(words, key=lambda w: w.start_ms)),
        speakers=_speakers(words, [], embedded),
        metadata_editable=False,
    )
