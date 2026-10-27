# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded recording observations and app-owned edits, independent of GTK."""

import array
import math
import os
import time

from .library_files import regular
from .meeting_store import LOCK, read_metadata, update_metadata
from .native_recorder import space_left
from .recording_session import strict_json


class SilenceClock:
    def __init__(self, now):
        self.last_sound = now

    def update(self, level, now):
        # Meters map -60..0 dBFS to 0..1. Above -55 dBFS counts as sound.
        if level > 5 / 60:
            self.last_sound = now
        return now - self.last_sound >= 20


def edit_recording(directory, *, title=None, notes=None):
    changes = {}
    if title is not None:
        value = title.strip()
        changes.update(title=value or None, titleSource="user" if value else "meeting-window")
    if notes is not None:
        changes["notes"] = notes
    with LOCK:
        # update_metadata rereads immediately under the same lock, retaining
        # transcript progress, unknown fields and independent edits.
        return update_metadata(directory, **changes)


def recording_observation(meeting):
    """Read <=100 ms per track. Never rewrite capture files or infer liveness."""
    levels, voices, rate = [0.0, 0.0], 0, 0
    descriptor = os.open(meeting.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with regular(descriptor, "session.json") as (stream, _):
            data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Recording manifest is too large")
            manifest = strict_json(data)
        kinds = {t["file"]: t for t in manifest.get("tracks", []) if isinstance(t, dict) and "file" in t}
        for track in meeting.tracks:
            description = kinds.get(track.path.name, {})
            if description.get("ended") is True:
                continue
            index = 0 if description.get("kind") in ("microphone", "local") else 1
            if description.get("kind") == "remote":
                voices += 1
            rate += track.sample_rate * track.channels * 2
            with regular(descriptor, track.path.name) as (stream, info):
                if time.time() - info.st_mtime > 1:
                    continue
                count = min(max(0, info.st_size - 44) // (2 * track.channels), track.sample_rate // 10)
                stream.seek(44 + max(0, (info.st_size - 44) // (2 * track.channels) - count) * 2 * track.channels)
                samples = array.array("h", stream.read(count * track.channels * 2))
                if os.sys.byteorder != "little":
                    samples.byteswap()
                rms = math.sqrt(sum((v / 32768) ** 2 for v in samples) / len(samples)) if samples else 0
                db = 20 * math.log10(rms) if rms else -60
                levels[index] = max(levels[index], max(0, min(1, (db + 60) / 60)))
    finally:
        os.close(descriptor)
    minutes, reserve = space_left(meeting.directory, rate or 48000 * 4)
    return dict(
        levels=levels, voices=voices, minutes=minutes, reserve=reserve, metadata=read_metadata(meeting.directory)
    )
