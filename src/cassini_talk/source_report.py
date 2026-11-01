# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated source facts for D52 details; presentation copy awaits R63."""

from dataclasses import dataclass
from fractions import Fraction

from .capture_report import from_session, stored


@dataclass(frozen=True)
class SourceFact:
    identity: str
    kind: str
    label: str
    outcome: str
    audio_ms: int | float
    padding_ms: Fraction | None
    dropped_ms: Fraction | None
    final: bool
    expected: bool = True

    @property
    def problem(self):
        # Normal startup cannot claim a participant was missed. An actual
        # source failure is actionable immediately, even if others continue.
        return self.expected and (self.outcome == "failed" or self.final and self.outcome in ("neverAudio", "lowAudio"))


def _counter(tracks, ids, field):
    total = Fraction(0)
    for identifier in dict.fromkeys(ids):
        track = tracks.get(identifier)
        if track is None:
            return None
        frames, rate = track.get(field, 0), track.get("sampleRate")
        if (
            type(frames) is not int
            or not 0 <= frames <= 9007199254740991
            or type(rate) is not int
            or not 8000 <= rate <= 192000
        ):
            return None
        total += Fraction(frames * 1000, rate)
    return total


def sources(session, metadata, *, live):
    raw_tracks = session.get("tracks", [])
    record = from_session(dict(session, tracks=raw_tracks if isinstance(raw_tracks, list) else []))
    record = stored(dict(recordingDetails=dict(captureRecord=record))) if record is not None else stored(metadata)
    if record is None:
        return ()
    # Whitelisted diagnostic tracks only. Never resolve paths or open files
    # named by a portable report. Conflicting duplicate facts are unknown.
    tracks = {}
    for track in record.get("tracks", []) if isinstance(record.get("tracks"), list) else []:
        if not isinstance(track, dict) or not isinstance(track.get("id"), str):
            continue
        identifier = track["id"]
        if identifier in tracks:
            tracks[identifier] = None
        else:
            tracks[identifier] = track
    result = []
    for row in record["sources"]:
        result.append(
            SourceFact(
                row["sourceTrackId"],
                row["kind"],
                " ".join(row["label"].split())[:180],
                row["captureOutcome"],
                row["audioMs"],
                _counter(tracks, row["trackIds"], "gapFrames"),
                _counter(tracks, row["trackIds"], "droppedFrames"),
                not live,
                expected=row.get("captureExpected", True),
            )
        )
    # These two counters may describe the same missing interval; they are not
    # summed into a fabricated total until ranges can establish their union.
    return tuple(result)
