# SPDX-License-Identifier: GPL-3.0-or-later
"""Physical WAV rotation inside one logical capture source; original PCM is retained."""

import errno
from uuid import uuid4

from .capture_clock import ClockAnchors, mapped_end_sample
from .recording_session import SourceIOError


class RotatingWAV:
    def __init__(self, session, identifier, writer):
        self.session, self.identifier, self.current = session, identifier, writer
        self.rate, self.channels = writer.rate, writer.channels
        self.base = 0
        self.rows = []
        self.span_clock = ClockAnchors(self.rate, session._native_clock_origin_ns)
        self.last_observation = None
        self.gap_frames = 0

    @property
    def frames(self):
        return self.base + self.current.frames

    @property
    def size(self):
        return self.frames * self.channels * 2

    @property
    def max_bytes(self):
        return self.current.max_bytes

    @property
    def file(self):
        return self.current.file

    @property
    def track(self):
        return next(t for t in self.session.manifest["tracks"] if t["id"] == self.identifier)

    def observe_clock(self, host_ns):
        self.span_clock.add(self.current.frames, host_ns)
        self.last_observation = self.frames, host_ns

    def snapshot(self):
        if not self.rows:
            return
        row = self.rows[-1]
        row["frames"] = self.current.frames
        if self.gap_frames:
            row.update(gapFrames=self.gap_frames, gapMaterialized=True)
        row["ended"] = bool(self.session.finished or self.track.get("ended"))
        anchors = self.span_clock.values()
        if anchors:
            row["clockAnchors"] = anchors
        self.track["frames"] = self.frames
        self.track.pop("clockAnchors", None)

    def _rotate(self):
        track = self.track
        self.current.close()
        if not self.rows:
            self.rows.append(
                {key: track[key] for key in ("file", "sampleRate", "channels", "startOffsetMs") if key in track}
            )
        self.rows[-1]["frames"] = self.current.frames
        self.rows[-1]["ended"] = True
        if self.gap_frames:
            self.rows[-1].update(gapFrames=self.gap_frames, gapMaterialized=True)
        anchors = self.span_clock.values()
        if anchors:
            self.rows[-1]["clockAnchors"] = anchors
        self.base += self.current.frames
        clock = self.session._native_clocks.get(self.identifier)
        offset = (
            mapped_end_sample(self.base, self.rate, track.get("startOffsetMs"), clock.values() if clock else ()) / 48
        )
        path = self.session.directory / f"track-{uuid4()}.wav"
        self.current = self.session.writer_factory(path, self.rate, self.channels)
        self.gap_frames = 0
        self.span_clock = ClockAnchors(self.rate, self.session._native_clock_origin_ns)
        if self.last_observation and self.last_observation[0] == self.base:
            self.span_clock.add(0, self.last_observation[1])
        self.rows.append(
            dict(file=path.name, sampleRate=self.rate, channels=self.channels, frames=0, startOffsetMs=offset)
        )
        track["spans"] = self.rows
        # Publish the reference and fsync the empty WAV before accepting any
        # new PCM. A crash can never strand an unreferenced later span.
        self.session.checkpoint()
        if self.identifier in self.session._failed_writers:
            # A negotiated checkpoint can retire/close this new WAV while
            # successfully persisting the rest of the capture. Do not resume
            # the pending PCM (or silence) append on that closed writer.
            raise SourceIOError(self.identifier, OSError(errno.EIO, "Audio source retired during WAV rotation"))

    def append(self, data):
        if len(data) > self.max_bytes:
            raise OSError("An audio packet exceeds the physical WAV span limit")
        if self.current.size + len(data) > self.max_bytes:
            self._rotate()
        self.current.append(data)

    def silence(self, frames):
        width = self.channels * 2
        limit = min(32768, self.max_bytes // width)
        if not limit:
            raise OSError("The WAV span limit cannot hold one complete frame")
        while frames:
            count = min(limit, frames)
            self.append(bytes(count * width))
            frames -= count

    def silence_gap(self, frames):
        width = self.channels * 2
        limit = min(32768, self.max_bytes // width)
        if not limit:
            raise OSError("The WAV span limit cannot hold one complete frame")
        while frames:
            count = min(limit, frames)
            before = self.frames
            try:
                self.append(bytes(count * width))
            finally:
                self.gap_frames += self.frames - before
            frames -= count

    def sync(self):
        self.current.sync()
        self.snapshot()

    def close(self):
        self.current.close()
        self.snapshot()
