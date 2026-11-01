# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated D52 source facts; levels and labels never establish identity."""

from dataclasses import dataclass
from fractions import Fraction

from .capture_report import from_session, stored
from .strings import text
from .ux_formats import meeting_duration


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

    @property
    def name(self):
        if self.kind in ("microphone", "local"):
            return text("common.yourMicrophone")
        if self.kind == "system":
            return text("recordFirst.sound.title")
        return self.label or text("details.source.unknown")

    @property
    def line(self):
        outcome = "notSelected" if not self.expected else self.outcome
        # During startup only actual failures warn. Partial live receipt is a
        # factual count, not a claim that this source has finished too short.
        if not self.final and outcome == "neverAudio":
            return self.name  # Announced, not yet received: make no success claim.
        if not self.final and outcome == "lowAudio":
            outcome = "captured"
        return text("details.sources." + outcome, source=self.name, duration=meeting_duration(self.audio_ms))

    @property
    def gap_lines(self):
        if not self.expected:
            return ()
        rows = []
        # Padding proves a hole in the saved timeline. Dropped packets can
        # describe that same hole: show unequal counters separately, never sum.
        if self.padding_ms:
            rows.append(
                text("details.sources.gap", source=self.name, duration=meeting_duration(float(self.padding_ms)))
            )
        if self.dropped_ms and self.dropped_ms != self.padding_ms:
            rows.append(text("details.health.dropped") + ": " + meeting_duration(float(self.dropped_ms)))
        return tuple(rows)


def live_problem(session, metadata=None):
    return any(row.problem for row in sources(session, metadata or {}, live=True))


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
