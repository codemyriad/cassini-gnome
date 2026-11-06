# SPDX-License-Identifier: GPL-3.0-or-later
"""D69 computer-side (writer) audio loss: the stored sentence and how it is shown.

The file keeps D69 as written: writer ranges, droppedFrames, the stored
problem sentence, state and end reason. D69.2 only changes what the app
shows: a recording whose only problem is writer loss is presented as a
normal one, with one quiet Recording details line per source whose summed
loss is at least 1 s. The stored sentence is for diagnostics and is never
shown. Presentation may still change, so it all lives here.
"""

import math
import re
from fractions import Fraction

# D69.2 point 3: shorter losses stay in the file and are not shown.
SHOWN_FROM_MS = 1000
NATIVE_KINDS = ("microphone", "system")


def sentence(milliseconds, kind):
    """The shared D69 problem sentence stored for one source (never shown, D69.2)."""
    return (
        f"{milliseconds} ms of {kind} audio was lost because the computer could not keep up; the gaps contain silence."
    )


STORED = re.compile(
    r"\d+ ms of [a-z]+ audio was lost because the computer could not keep up; the gaps contain silence\."
)


def source_key(track):
    """D69: one source per native kind (a reselection or fallback reopens a span
    with a fresh id and no sourceTrackId), per sourceTrackId, else per track."""
    if track.get("kind") in NATIVE_KINDS:
        return ("native", track["kind"])
    if track.get("sourceTrackId") is not None:
        return ("source", track["sourceTrackId"])
    return ("track", track.get("id"))


def rounded(duration):
    """Half-up once per source, as the stored sentence rounds."""
    return math.floor(duration + Fraction(1, 2))


def _rate(track):
    rate = track.get("sampleRate")
    return rate if type(rate) is int and 8000 <= rate <= 192000 else None


def _frames(value):
    return value if type(value) is int and 0 <= value <= 9007199254740991 else None


def source_losses(tracks):
    """[(first track of the source, exact ms)] of writer loss from the stored
    writer ranges, summed per source; sources without writer loss are left out."""
    totals = {}
    for track in tracks if isinstance(tracks, list) else ():
        if not isinstance(track, dict) or (rate := _rate(track)) is None:
            continue
        gaps = track.get("gaps") if isinstance(track.get("gaps"), list) else ()
        frames = sum(
            _frames(gap.get("frames")) or 0 for gap in gaps if isinstance(gap, dict) and gap.get("kind") == "writer"
        )
        if frames:
            first, total = totals.get(source_key(track), (track, Fraction(0)))
            totals[source_key(track)] = first, total + Fraction(frames * 1000, rate)
    return list(totals.values())


def shown_losses(tracks):
    """D69.2: the sources whose summed writer loss is at least 1 s, as (track, ms)."""
    return [(track, rounded(total)) for track, total in source_losses(tracks) if total >= SHOWN_FROM_MS]


def writer_only(session):
    """The recording's only problem is D69 writer loss: no other stored problem,
    no input gap of 1 s or more, no failed track and no gap-budget overflow."""
    if not isinstance(session, dict) or not source_losses(session.get("tracks")):
        return False
    problem = session.get("problem")
    if problem is not None and (not isinstance(problem, str) or STORED.sub("", problem).strip()):
        return False
    if session.get("problemKind") not in (None, "io"):
        return False
    for track in session.get("tracks", []):
        if not isinstance(track, dict):
            continue
        if track.get("problemKind") is not None:
            return False
        rate = _rate(track)
        for gap in track.get("gaps") or ():
            if not isinstance(gap, dict) or gap.get("kind") in ("writer", "prefix"):
                continue
            frames = _frames(gap.get("frames"))
            if gap.get("kind") == "overflow" or rate is None or frames is None or frames >= rate:
                return False
    return True
