# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep a wire source's validation separate from its selected recording spans."""

import math
from dataclasses import dataclass, replace
from uuid import uuid4

from .recording_session import ProtocolError, chunk_plan, source_parameters

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
    start: float | None = None
    frames: int = 0
    recorded: str | None = None
    boundary: float = 0


class BrowserStreams:
    def __init__(self, session, *, me=True, others=True):
        self.session = session
        self.me, self.others, self.browser = me, others, True
        self.tracks = {}

    def enabled(self, track):
        return self.browser and (self.me if track.kind in ME_KINDS else self.others)

    def _record(self, identifier, track, boundary=None):
        stored = identifier if identifier not in self.session.writers else "span-" + str(uuid4())
        self.session.add_track(stored, track.label, track.kind, track.rate, track.channels, label_source="browser")
        track.recorded, track.boundary = stored, boundary or 0
        if boundary is not None:
            self.session.manifest["tracks"][-1]["startOffsetMs"] = boundary

    def add_track(self, identifier, label, kind, rate, channels):
        label = source_parameters(identifier, label, kind, rate, channels, self.tracks)
        track = WireTrack(label, kind, rate, channels)
        if self.enabled(track):
            self._record(identifier, track)
        self.tracks[identifier] = track

    def append(self, identifier, data, offset):
        track = self.tracks[identifier]
        start, padding = chunk_plan(track.rate, track.channels, track.frames, track.start, data, offset)
        frames = track.frames + padding + len(data) // (track.channels * 2)
        if frames * track.channels * 2 > 0xFFFFFFFF - 36:
            raise ProtocolError("limit", "Track reached the WAV size limit. Start a new recording.")
        if track.recorded is not None:
            selected, selected_offset = selected_pcm(data, offset, track.rate, track.channels, track.boundary)
            if selected:
                self.session.append(track.recorded, selected, selected_offset)
        track.start, track.frames = start, frames

    def select(self, me, others, at, *, browser=True):
        """Called inside the owner's atomic event/track change and capture lock."""
        previous = self.me, self.others, self.browser, {key: replace(t) for key, t in self.tracks.items()}
        try:
            self.me, self.others, self.browser = me, others, browser
            additions = sum(t.recorded is None and self.enabled(t) for t in self.tracks.values())
            if len(self.session.writers) + additions > 64:
                raise ProtocolError("limit", "A session supports at most 64 sources")
            for identifier, track in self.tracks.items():
                if track.recorded is not None and not self.enabled(track):
                    self.session.end_track(track.recorded)
                    track.recorded = None
                elif track.recorded is None and self.enabled(track):
                    self._record(identifier, track, at)
        except BaseException:
            self.me, self.others, self.browser, self.tracks = previous
            raise
