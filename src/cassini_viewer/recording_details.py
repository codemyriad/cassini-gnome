# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded recording observations and app-owned edits, independent of GTK."""

import array
import math
import os
import time

from .library import LibraryScanner, meeting_id
from .library_edits import mutation as transaction
from .library_files import regular
from .meeting_store import update_metadata


def ac_rms(samples, channels=1):
    """Per-channel mean removal for observation only; PCM is never changed."""
    energy = 0.0
    for channel in range(channels):
        values = samples[channel::channels]
        count = len(values)
        if count:
            total = sum(values)
            energy += max(0, sum(v * v for v in values) * count - total * total) / (count * count)
    return math.sqrt(energy / channels) / 32768


def pcm_ac_level(data, channels=1):
    samples = array.array("h", data)
    if os.sys.byteorder != "little":
        samples.byteswap()
    rms = ac_rms(samples, channels)
    db = 20 * math.log10(rms) if rms else -60
    return max(0.0, min(1.0, (db + 60) / 60))


class ACMeter:
    """100 ms PCM windows, independent of appsink buffer boundaries."""

    def __init__(self, rate=48000, channels=1):
        self.width = rate // 10 * channels * 2
        self.channels = channels
        self.pending = bytearray()
        self.level = 0.0

    def feed(self, data):
        self.pending.extend(data)
        changed = len(self.pending) >= self.width
        while len(self.pending) >= self.width:
            self.level = pcm_ac_level(self.pending[: self.width], self.channels)
            del self.pending[: self.width]
        return changed


def disk_reserve(total):
    """D7 Q5: the space Cassini never fills, the larger of 1 GiB and 2 % of the volume."""
    return max(1024**3, math.ceil(total * 0.02))


def space_left(path, byte_rate=48000 * 4, statvfs=None):
    import os

    value = (statvfs or os.statvfs)(path)
    total, free = value.f_blocks * value.f_frsize, value.f_bavail * value.f_frsize
    reserve = disk_reserve(total)
    return max(0, math.floor((free - reserve) / byte_rate / 60)), free <= reserve


class SilenceClock:
    def __init__(self, now):
        self.last_sound = now

    def update(self, level, now):
        # Meters map -60..0 dBFS to 0..1. Above -55 dBFS counts as sound.
        if level > 5 / 60:
            self.last_sound = now
        return now - self.last_sound >= 20


def edit_recording(directory, *, title=None, notes=None):
    from pathlib import Path

    directory = Path(directory).absolute()
    changes = {}
    if title is not None:
        value = title.strip()[:500]
        changes.update(title=value or None, titleSource="user" if value else "meeting-window")
    if notes is not None:
        changes["notes"] = notes
    from .library_location import owned_directory

    directory = owned_directory(directory)
    with transaction(directory):
        return update_metadata(directory, **changes)


def recording_observation(meeting, *, freshness_seconds=1):
    """Read <=one second per track; meters use the latest 100 ms. Never rewrite capture files or infer liveness."""
    levels, rate = [0.0, 0.0], 0
    voices = sum(t.kind == "remote" and t.ended is not True for t in meeting.tracks)
    recent_sound = False
    descriptor = os.open(meeting.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for track in (span for logical in meeting.tracks for span in (logical.spans or (logical,))):
            # Live meters read growing WAVs only; compacted spans are never live.
            if track.ended is True or track.codec != "pcm":
                continue
            index = 0 if track.kind in ("microphone", "local") else 1
            rate += track.sample_rate * track.channels * 2
            with regular(descriptor, track.path.name) as (stream, info):
                if freshness_seconds is not None and time.time() - info.st_mtime > freshness_seconds:
                    continue
                count = min(max(0, info.st_size - 44) // (2 * track.channels), track.sample_rate)
                stream.seek(44 + max(0, (info.st_size - 44) // (2 * track.channels) - count) * 2 * track.channels)
                samples = array.array("h", stream.read(count * track.channels * 2))
                if os.sys.byteorder != "little":
                    samples.byteswap()
                width = max(1, track.sample_rate // 10 * track.channels)
                powers = [
                    ac_rms(samples[first : first + width], track.channels) for first in range(0, len(samples), width)
                ]
                rms = powers[-1] if powers else 0
                if index == 0 and any(value > 10 ** (-55 / 20) for value in powers):
                    recent_sound = True
                db = 20 * math.log10(rms) if rms else -60
                levels[index] = max(levels[index], max(0, min(1, (db + 60) / 60)))
    finally:
        os.close(descriptor)
    minutes, reserve = space_left(meeting.directory, rate or 48000 * 4)
    from .output_mute import currently_muted

    return dict(
        levels=levels,
        voices=voices,
        minutes=minutes,
        reserve=reserve,
        recent_sound=recent_sound,
        output_muted=currently_muted(meeting.metadata),
    )


def live_meeting(directory, phase="recording"):
    """Read only a controller-reported capture, including without a Library window."""
    from pathlib import Path

    from .library_location import owned_directory

    directory = owned_directory(Path(directory))
    identifier = meeting_id(directory)
    return next(
        (
            m
            for m in LibraryScanner.for_meeting(directory)
            .scan(changed_directories={directory}, capture_activity={identifier: phase})
            .meetings
            if m.directory == directory
        ),
        None,
    )


def live_observation(meeting):
    # New/ended browser tracks can appear while only the Browser window exists.
    # The scanner validates identities before the bounded PCM observation.
    current = live_meeting(meeting.directory, meeting.lifecycle)
    if current is None:
        raise ValueError("Live recording is no longer readable")
    return current, recording_observation(current)
