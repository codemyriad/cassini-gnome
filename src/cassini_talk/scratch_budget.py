# SPDX-License-Identifier: GPL-3.0-or-later
"""G14/R75: the disk budget for the speech child's temporary rejoin audio.

No NumPy: the test suite's conftest fakes free space through `volume_usage`.
"""

import os
from pathlib import Path

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


def volume_usage(path):
    """os.statvfs. The test suite replaces it so tests never depend on real free space."""
    return os.statvfs(path)


def shortfall(duration_ms, *, scratch=None, statvfs=None, recordings=()):
    """G14/R75: None when one rejoin of this group fits, else the numbers.

    Needed is the rejoin size from the group's metadata extent, plus one second
    for rounding, so no audio is read. The reserve is a 1 GiB floor. The parent
    passes the folders of recordings live at job start; when one is on this
    volume, the recorder's D7 Q5 reserve max(1 GiB, 2 %) applies instead. A
    recording that starts mid-job is covered only by the floor and by the
    recorder's own low-disk handling: an accepted gap.
    """
    from .recording_details import disk_reserve

    statvfs = statvfs or volume_usage
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
