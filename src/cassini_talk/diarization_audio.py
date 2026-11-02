# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 rejoin on disk: stream/downmix/FIR, silence gaps and sum track spans.

Only the speech child imports this module. Long received audio never becomes a
WAV in /tmp or a concatenated in-memory PCM array.
"""

import hashlib
import math
import os
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

from .speech_pipeline import audio_chunks

# One rejoin file is float32 at 16 kHz: 64 bytes per millisecond of meeting.
BYTES_PER_MS = 4 * 16


def scratch_directory(scratch=None):
    directory = (
        Path(scratch)
        if scratch
        else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cassini/diarization"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


# Always kept free for everything else, even with no recording running.
FLOOR = 1024**3


def live_recordings(options):
    """The `recordingDirectories` option: folders of recordings live when the parent made the job."""
    value = options.get("recordingDirectories")
    return [v for v in value[:64] if isinstance(v, str) and v] if isinstance(value, list) else []


def recording_here(directory, recordings, statvfs):
    """Whether one of the recordings that were live at job start shares this volume.

    Same device, or the same total size, which also covers btrfs subvolumes of one
    filesystem; a false match only keeps more space free.
    """
    here = os.stat(directory).st_dev, statvfs(directory).f_blocks * statvfs(directory).f_frsize
    for recording in recordings:
        try:
            there = os.stat(recording).st_dev, statvfs(recording).f_blocks * statvfs(recording).f_frsize
        except (OSError, TypeError, ValueError):
            continue  # Already gone, or not a path.
        if there[0] == here[0] or there[1] == here[1]:
            return True
    return False


def shortfall(duration_ms, *, scratch=None, statvfs=os.statvfs, recordings=()):
    """G14/R75: None when one rejoin of this group fits, else the numbers.

    Needed is the rejoin size from the group's metadata extent, plus one second
    for rounding, so no audio is read. The reserve is a 1 GiB floor. The parent
    passes the folders of recordings live at job start; when one is on this
    volume, the recorder's D7 Q5 reserve max(1 GiB, 2 %) applies instead. A
    recording that starts mid-job is covered only by the floor and by the
    recorder's own low-disk handling: an accepted gap.
    """
    from .recording_details import disk_reserve

    directory = scratch_directory(scratch)
    usage = statvfs(directory)
    free = usage.f_bavail * usage.f_frsize
    needed = (max(0, duration_ms) + 1000) * BYTES_PER_MS
    reserve = FLOOR
    if recordings and recording_here(directory, recordings, statvfs):
        reserve = max(FLOOR, disk_reserve(usage.f_blocks * usage.f_frsize))
    if free >= needed + reserve:
        return None
    return dict(freeBytes=free, neededBytes=needed, reserveBytes=reserve)


@contextmanager
def rejoin(tracks, *, scratch=None, heartbeat=lambda: None):
    directory = scratch_directory(scratch)
    last_notice = time.monotonic()
    # A file descriptor avoids transporting paths to temporary audio; no
    # transcript/capture file is opened for writing by the worker.
    with tempfile.TemporaryFile(dir=directory) as stream:
        frames = 0
        for track in tracks:
            offset = track.get("offsetMs", track.get("startOffsetMs")) or 0
            if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
                raise ValueError("Invalid diarization track offset")
            cursor = math.floor(offset * 16)
            with closing(audio_chunks(track)) as chunks:
                for block in chunks:
                    if not len(block):
                        continue
                    if len(block) > 16000 or block.ndim != 1 or not np.isfinite(block).all():
                        raise ValueError("Invalid diarization PCM block")
                    stream.seek(cursor * 4)
                    previous = stream.read(len(block) * 4)
                    mixed = np.asarray(block, dtype="<f4").copy()
                    if previous:
                        mixed[: len(previous) // 4] += np.frombuffer(previous, dtype="<f4")
                    stream.seek(cursor * 4)
                    stream.write(mixed.tobytes())
                    cursor += len(block)
                    frames = max(frames, cursor)
                    if time.monotonic() - last_notice >= 1:
                        heartbeat()
                        last_notice = time.monotonic()
        stream.flush()
        stream.seek(0)
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        yield stream, frames, fingerprint
