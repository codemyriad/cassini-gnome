# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional D28 draft bookkeeping in app-owned meeting.json, never session events."""

import os
from fractions import Fraction
from pathlib import Path

from .library_files import regular
from .meeting_store import LOCK, read_metadata, update_metadata
from .recording_session import strict_json

MAX_INTEGER = 9007199254740991


def record_end(directory, reason, *, manifest=None):
    """Called only by the owning recorder or after a validated manual Stop."""
    if reason not in ("user", "disk", "appQuit", "error"):
        raise ValueError("Unsupported recording end reason")
    directory = Path(directory)
    if not directory.name.startswith("Recording-"):
        return False
    if not directory.exists():
        from .library_location import owned_directory

        directory = owned_directory(directory)
    if manifest is None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with regular(descriptor, "session.json") as (stream, info):
                if info.st_size > 1024 * 1024:
                    raise ValueError("Capture metadata exceeds limit")
                manifest = strict_json(stream.read(1024 * 1024 + 1))
        finally:
            os.close(descriptor)
    if not isinstance(manifest, dict) or manifest.get("state") not in ("completed", "interrupted"):
        return False
    if manifest.get("problemKind") not in (None, "gap"):
        reason = "error"
    ends = []
    tracks = manifest.get("tracks")
    if not isinstance(tracks, list):
        return False
    for track in tracks[:64]:
        if not isinstance(track, dict):
            continue
        frames, rate, offset = track.get("frames"), track.get("sampleRate"), track.get("startOffsetMs")
        if (
            type(frames) is not int
            or not 0 <= frames <= MAX_INTEGER
            or type(rate) is not int
            or not 8000 <= rate <= 192000
        ):
            continue
        if type(offset) not in (int, float) or not 0 <= offset <= 86400000:
            continue
        ends.append(int(Fraction(str(offset)) + Fraction(frames * 1000, rate)))
    end = dict(reason=reason)
    if ends and max(ends) <= MAX_INTEGER:
        end["atMs"] = max(ends)
    with LOCK:
        current = read_metadata(directory)
        details = dict(current.get("recordingDetails", {}))
        previous = dict(details.get("end", {}))
        previous.pop("atMs", None)  # Unknown current timing must not retain an older endpoint.
        end = dict(previous, **end)
        if details.get("end") == end:
            return False
        details["end"] = end
        update_metadata(directory, recordingDetails=details)
    return True


def writing_details(previous, facts):
    """Replace this attempt's known facts while retaining future additive fields."""
    details = dict(previous)
    writing = dict(details.get("writingDown", {}))
    separation = dict(writing.pop("speakerSeparation", {}))
    for key in ("status", "modelId", "modelRevision", "speakersFound", "reused", "reason"):
        separation.pop(key, None)
    for key in ("wallDurationMs", "resumed", "speechSegments"):
        writing.pop(key, None)
    details["writingDown"] = dict(
        writing, **dict(facts, speakerSeparation=dict(separation, **facts["speakerSeparation"]))
    )
    return details


def separation_unavailable(models, tracks, origin):
    from .diarization import group_tracks

    if not any(group["eligible"] for group in group_tracks(tracks, origin=origin)):
        reason = "notApplicable"
    elif models is None:
        reason = "unavailable"
    else:
        state = models.snapshot() if hasattr(models, "snapshot") else {}
        reason = "modelsNotInstalled" if state.get("downloaded") is False else "modelNotReady"
    return dict(status="skipped", reason=reason)


def separation_result(result, groups, reused):
    from .diarization import usable
    from .nemotron_model import MODEL_ID, MODEL_TAG, REVISION

    if not isinstance(result, dict) or result.get("model") != MODEL_TAG or not groups:
        return dict(status="failed", reason="processingFailed")
    if all(group["durationMs"] < 200 for group in groups):
        return dict(status="skipped", reason="tooShort", modelId=MODEL_ID, modelRevision=REVISION)
    voices = {(group["id"], turn["speaker"]) for group in groups for turn in group["turns"] if usable(turn)}
    if len(voices) > 1000:
        return dict(status="failed", reason="processingFailed")
    partial = bool(result.get("diagnostics"))
    facts = dict(
        status="partial" if partial else "completed",
        modelId=MODEL_ID,
        modelRevision=REVISION,
        speakersFound=len(voices),
        reused=all(group["id"] in reused for group in groups),
    )
    if partial:
        facts["reason"] = "processingFailed"
    return facts
