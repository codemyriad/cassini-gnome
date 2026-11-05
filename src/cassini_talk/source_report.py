# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated D52 source facts; levels and labels never establish identity."""

from dataclasses import dataclass
from fractions import Fraction

from .capture_report import from_session, stored
from .strings import text
from .ux_formats import details_duration


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
    track_ids: tuple[str, ...] = ()

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
    def under_second(self):
        """D58.4: a finished low-audio source under 1 s says so in words, not as a number."""
        return self.expected and self.final and self.outcome == "lowAudio" and self.audio_ms < 1000

    @property
    def line_id(self):
        """The catalog id of the Audio sources line this source shows (shared test 8).

        It names the line actually shown: a live lowAudio source reads as captured,
        a not-selected one as notSelected, and a live source not yet heard shows
        only its name, so it has no catalog id ("")."""
        outcome = "notSelected" if not self.expected else self.outcome
        # During startup only actual failures warn. Partial live receipt is a
        # factual count, not a claim that this source has finished too short.
        if not self.final and outcome == "neverAudio":
            return ""  # Announced, not yet received: make no success claim.
        if not self.final and outcome == "lowAudio":
            return "details.sources.captured"
        if self.under_second:
            return "details.sources.lowAudio.underSecond"
        return "details.sources." + outcome

    @property
    def line(self):
        line_id = self.line_id
        if not line_id:
            return self.name
        if self.under_second:
            return text(line_id, source=self.name)
        return text(line_id, source=self.name, duration=details_duration(self.audio_ms))

    def silent(self, silent_tracks):
        """D67.1 (LIBRARY-RULES R13): a finished, captured source whose every track the
        write-down listed in writingDown.silentTracks. A never-captured source keeps its
        neverAudio line instead, though its 0-frame track is listed too (D67.3)."""
        return (
            self.expected
            and self.final
            and self.outcome == "captured"
            and bool(self.track_ids)
            and all(identifier in silent_tracks for identifier in self.track_ids)
        )

    @property
    def gap_lines(self):
        if not self.expected:
            return ()
        rows = []
        # Padding proves a hole in the saved timeline. Dropped packets can
        # describe that same hole: show unequal counters separately, never sum.
        if self.padding_ms:
            rows.append(
                text("details.sources.gap", source=self.name, duration=details_duration(float(self.padding_ms)))
            )
        if self.dropped_ms and self.dropped_ms != self.padding_ms:
            rows.append(text("details.health.dropped") + ": " + details_duration(float(self.dropped_ms)))
        return tuple(rows)


def live_problem(session, metadata=None):
    if any(row.problem for row in sources(session, metadata or {}, live=True)):
        return True
    # Native microphone/system inputs have no browser discovery row. A failed
    # physical WAV proves the source was selected, even with no received PCM.
    tracks = session.get("tracks")
    return isinstance(tracks, list) and any(
        isinstance(track, dict)
        and track.get("kind") in ("microphone", "system")
        and track.get("ended") is True
        and track.get("problemKind") == "io"
        for track in tracks
    )


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
    # Source rows come from captureOutcome. A session with only refusals (D58)
    # has none of its own: the stored captureRecord's rows stay authoritative.
    record = (
        from_session(dict(session, tracks=raw_tracks if isinstance(raw_tracks, list) else []))
        if isinstance(session.get("captureOutcome"), dict)
        else None
    )
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
                track_ids=tuple(row["trackIds"]),
            )
        )
    # These two counters may describe the same missing interval; they are not
    # summed into a fabricated total until ranges can establish their union.
    return tuple(result)
