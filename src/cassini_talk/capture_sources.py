# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep a wire source's validation separate from its selected recording spans."""

import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from uuid import uuid4

from .recording_session import ProtocolError, chunk_plan, source_parameters, source_track_identity

ME_KINDS = {"microphone", "local"}


def track_end(track):
    return (track.get("startOffsetMs") or 0) + track["frames"] * 1000 / track["sampleRate"]


def change_time(session, elapsed):
    """A new span cannot overlap frames already accepted on a source clock."""
    return max(
        [
            elapsed,
            *(track_end(t) for t in session.manifest["tracks"]),
            *(e["atMs"] for e in session.manifest.get("events", [])),
        ]
    )


def selected_pcm(data, offset, rate, channels, start):
    """Drop queued pre-selection frames without moving their original timestamps."""
    skip = max(0, math.ceil((start - offset) * rate / 1000))
    return data[skip * channels * 2 :], offset + skip * 1000 / rate


@dataclass
class WireTrack:
    label: str
    kind: str
    rate: int
    channels: int
    source_track_id: str | None = None
    ended: bool = False
    start: float | None = None
    frames: int = 0
    recorded: str | None = None
    boundary: float = 0
    available: bool = True
    received_at: float | None = None
    recorded_at: float | None = None


class BrowserStreams:
    def __init__(self, session, *, me=True, others=True, picture_enabled=None):
        self.session = session
        self.me, self.others, self.browser = me, others, True
        self.tracks = {}
        self.selection_at = 0
        self.picture_enabled = picture_enabled
        self.picture = None

    def enabled(self, track):
        return (
            self.browser
            and track.available
            and not track.ended
            and (self.me if track.kind in ME_KINDS else self.others)
        )

    def additions(self, me, others, *, browser=True):
        return sum(
            t.recorded is None and browser and t.available and not t.ended and (me if t.kind in ME_KINDS else others)
            for t in self.tracks.values()
        )

    def _record(self, identifier, track, boundary=None):
        stored = identifier if identifier not in self.session.writers else "span-" + str(uuid4())
        source_track_id = track.source_track_id
        if source_track_id is None and track.kind not in ME_KINDS and track.kind != "system":
            source_track_id = identifier
        self.session.add_track(
            stored,
            track.label,
            track.kind,
            track.rate,
            track.channels,
            label_source="browser",
            source_track_id=source_track_id,
        )
        track.recorded, track.boundary = stored, boundary or 0
        track.recorded_at = None
        if boundary is not None:
            self.session.manifest["tracks"][-1]["startOffsetMs"] = boundary
        self.session.checkpoint()

    def add_track(self, identifier, label, kind, rate, channels, *, source_track_id=None):
        label = source_parameters(identifier, label, kind, rate, channels, self.tracks)
        source_track_id = source_track_identity(source_track_id)
        track = WireTrack(label, kind, rate, channels, source_track_id=source_track_id, available=self.browser)
        if self.enabled(track):
            with self.session.change([]):
                self._record(identifier, track)
        self.tracks[identifier] = track

    def end_track(self, identifier):
        track = self.tracks[identifier]
        try:
            if track.recorded is not None:
                self.session.end_track(track.recorded)
        finally:
            track.recorded = None
            track.recorded_at = None
            track.ended = True
            track.available = False

    def append(self, identifier, data, offset):
        self.picture = None
        track = self.tracks[identifier]
        if track.ended:
            raise ProtocolError("source", "Audio source has ended")
        start, padding = chunk_plan(track.rate, track.channels, track.frames, track.start, data, offset)
        frames = track.frames + padding + len(data) // (track.channels * 2)
        if frames * track.channels * 2 > 0xFFFFFFFF - 36:
            raise ProtocolError("limit", "Track reached the WAV size limit. Start a new recording.")
        track.available, track.received_at = True, time.monotonic()
        if track.recorded is None and self.enabled(track):
            # A returning document may expose sources after the first restored
            # one. Open each span on real PCM, never an obsolete declaration.
            with self.change(), self.session.change([]):
                self._record(identifier, track, max(self.selection_at, offset))
        if track.recorded is not None:
            selected, selected_offset = selected_pcm(data, offset, track.rate, track.channels, track.boundary)
            if selected:
                self.session.append(track.recorded, selected, selected_offset)
                track.recorded_at = time.monotonic()
                if self.picture_enabled is not None and self.picture_enabled():
                    record = next(t for t in self.session.manifest["tracks"] if t["id"] == track.recorded)
                    spans = record.get("spans") or [record]
                    start_ms = spans[0].get("startOffsetMs")
                    self.picture = dict(
                        identifier=track.recorded,
                        data=selected,
                        offset_ms=selected_offset,
                        rate=track.rate,
                        channels=track.channels,
                        kind=track.kind,
                        start_ms=selected_offset if start_ms is None else start_ms,
                        level=None,
                    )
        track.start, track.frames = start, frames

    @contextmanager
    def change(self):
        """Roll back wire selection with the owner's event/track transaction."""
        ledger = self.session.capture_record
        expectations = {key: row.get("expected", True) for key, row in ledger.seen.items()} if ledger else {}
        bindings = dict(ledger.bindings) if ledger else {}
        previous = (
            self.me,
            self.others,
            self.browser,
            self.selection_at,
            {key: replace(t) for key, t in self.tracks.items()},
        )
        try:
            yield
        except BaseException:
            self.me, self.others, self.browser, self.selection_at, self.tracks = previous
            if ledger:
                for key, expected in expectations.items():
                    ledger.seen[key]["expected"] = expected
                ledger.bindings = bindings
            raise

    def select(self, me, others, at, *, browser=True):
        """Called inside the owner's atomic event/track change and capture lock."""
        with self.change():
            if self.browser and not browser:
                for track in self.tracks.values():
                    track.available = False
            self.me, self.others, self.browser = me, others, browser
            self.selection_at = at
            additions = sum(t.recorded is None and self.enabled(t) for t in self.tracks.values())
            if len(self.session.writers) + additions > 64:
                raise ProtocolError("limit", "A session supports at most 64 sources")
            for identifier, track in self.tracks.items():
                if track.recorded is not None and not self.enabled(track):
                    self.session.end_track(track.recorded)
                    track.recorded = None
                elif track.recorded is None and self.enabled(track):
                    self._record(identifier, track, at)
            if self.session.capture_record is not None:
                self.session.capture_record.select(me=me, others=others, browser=browser)
