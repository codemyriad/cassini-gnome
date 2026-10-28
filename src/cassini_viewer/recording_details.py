# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded recording observations and app-owned edits, independent of GTK."""

import array
import math
import os
import time

from .library import LibraryScanner, meeting_id
from .library_files import regular
from .meeting_store import LOCK, update_metadata


def space_left(path, byte_rate=48000 * 4, statvfs=None):
    import os

    value = (statvfs or os.statvfs)(path)
    total, free = value.f_blocks * value.f_frsize, value.f_bavail * value.f_frsize
    reserve = max(1024**3, math.ceil(total * 0.02))
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
        value = title.strip()
        changes.update(title=value or None, titleSource="user" if value else "meeting-window")
    if notes is not None:
        changes["notes"] = notes
    with LOCK:
        from .library_location import owned_directory

        directory = owned_directory(directory)
        # update_metadata rereads immediately under the same lock, retaining
        # transcript progress, unknown fields and independent edits.
        return update_metadata(directory, **changes)


def recording_observation(meeting, *, freshness_seconds=1):
    """Read <=one second per track; meters use the latest 100 ms. Never rewrite capture files or infer liveness."""
    levels, voices, rate = [0.0, 0.0], 0, 0
    recent_sound = False
    descriptor = os.open(meeting.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for track in meeting.tracks:
            if track.ended is True:
                continue
            index = 0 if track.kind in ("microphone", "local") else 1
            if track.kind == "remote":
                voices += 1
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
                    math.sqrt(
                        sum((v / 32768) ** 2 for v in samples[first : first + width])
                        / len(samples[first : first + width])
                    )
                    for first in range(0, len(samples), width)
                ]
                rms = powers[-1] if powers else 0
                if index == 0 and any(value > 10 ** (-55 / 20) for value in powers):
                    recent_sound = True
                db = 20 * math.log10(rms) if rms else -60
                levels[index] = max(levels[index], max(0, min(1, (db + 60) / 60)))
    finally:
        os.close(descriptor)
    minutes, reserve = space_left(meeting.directory, rate or 48000 * 4)
    return dict(levels=levels, voices=voices, minutes=minutes, reserve=reserve, recent_sound=recent_sound)


def live_meeting(directory, phase="recording"):
    """Read only a controller-reported capture, including without a Library window."""
    from pathlib import Path

    from .library_location import owned_directory

    directory = owned_directory(Path(directory))
    identifier = meeting_id(directory)
    return next(
        (
            m
            for m in LibraryScanner(directory.parent)
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
