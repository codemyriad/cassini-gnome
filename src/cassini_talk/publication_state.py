# SPDX-License-Identifier: GPL-3.0-or-later
"""D48 facts and actions for retained audio and published Library files."""

import errno
from dataclasses import replace

from .library_v2 import journal


def session_root_path(directory):
    """Derive a display path from an already-scanned row, without filesystem IO.

    This is not an ownership check. Retry/publication workers still validate the
    routed directory and reject symlinks before touching a session.
    """
    if (
        journal.identity_of(directory) is not None
        and directory.parent.name == "sessions"
        and directory.parent.parent.name == journal.META
    ):
        return directory.parent.parent.parent
    return None


def failure_notice(error):
    # Inspect the internal error identity without importing the NumPy/GStreamer
    # encoder on GTK's main thread. The worker retains the actual exception.
    kind = (type(error).__module__, type(error).__name__)
    if kind == ("cassini_talk.library_v2.mixdown", "EmptyRecording"):
        return "library.publishNoAudio"
    if kind == ("cassini_talk.library_v2.finalize", "LowDisk") or (
        isinstance(error, OSError) and error.errno == errno.ENOSPC
    ):
        return "library.publishDiskLow"
    return "library.publishFailed"


def project(meeting, error=None):
    if meeting.lifecycle != "saved":
        return meeting
    notice = None
    if meeting.published_missing:
        notice = "library.publishedMissing"
    elif error is not None:
        notice = failure_notice(error)
    elif meeting.published_file is not None:
        ours = meeting.extensions.get("codemyriad.io", {})
        tracks = ours.get("tracks", []) if isinstance(ours, dict) else []
        if any(
            isinstance(track, dict)
            and isinstance(track.get("spans"), list)
            and track["spans"]
            and all(isinstance(span, dict) and span.get("frames") == 0 for span in track["spans"])
            for track in (tracks if isinstance(tracks, list) else [])
        ) or any(track.frames == 0 for track in meeting.tracks):
            notice = "library.publishSourcesEmpty"
    elif (
        session_root_path(meeting.directory) is not None
        and meeting.capture_state in ("completed", "interrupted")
        and meeting.tracks
        and all(track.frames == 0 for track in meeting.tracks)
    ):
        notice = "library.publishNoAudio"
    return replace(meeting, publication_notice=notice) if notice != meeting.publication_notice else meeting
