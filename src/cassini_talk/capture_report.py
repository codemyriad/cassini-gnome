# SPDX-License-Identifier: GPL-3.0-or-later
"""D52 capture facts carried with the file; absence means unknown, never empty."""

import math
from copy import deepcopy

from .meeting_store import metadata_transaction, read_metadata, update_metadata

TRACK_FIELDS = (
    "id",
    "kind",
    "label",
    "sourceTrackId",
    "frames",
    "sampleRate",
    "channels",
    "startOffsetMs",
    "endOffsetMs",
    "gapFrames",
    "droppedFrames",
    "problemKind",
    "captureOutcome",
    "receivedFrames",
)


def from_session(session):
    """Copy facts, never infer missing audio from levels, labels or silence."""
    outcome = session.get("captureOutcome")
    if not isinstance(outcome, dict) or not isinstance(outcome.get("sources"), list):
        return None
    result = dict(
        deepcopy(outcome),
        version=1,
        sources=deepcopy(outcome["sources"]),
        tracks=[
            {key: deepcopy(track[key]) for key in TRACK_FIELDS if key in track}
            for track in session.get("tracks", [])
            if isinstance(track, dict)
        ],
        events=deepcopy(session.get("events", [])),
    )
    if "problemKind" in session:
        result["problemKind"] = session["problemKind"]
    return result


def _opaque(value):
    if not isinstance(value, str):
        return False
    try:
        return 0 < len(value.encode("utf-8")) <= 200
    except UnicodeError:
        return False


def stored(metadata):
    details = metadata.get("recordingDetails")
    record = details.get("captureRecord") if isinstance(details, dict) else None
    if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1:
        return None
    sources = record.get("sources")
    if not isinstance(sources, list):
        return None
    from .recording_session import KINDS

    for row in sources:
        if not isinstance(row, dict):
            return None
        identity, duration, ids = row.get("sourceTrackId"), row.get("audioMs"), row.get("trackIds")
        if (
            not _opaque(identity)
            or not isinstance(row.get("kind"), str)
            or row["kind"] not in KINDS
            or not isinstance(row.get("label"), str)
            or len(row["label"]) > 200
            or row.get("captureOutcome") not in ("captured", "neverAudio", "lowAudio", "failed")
            or "captureExpected" in row
            and type(row["captureExpected"]) is not bool
            or type(duration) not in (int, float)
            or not 0 <= duration <= 9007199254740991
            or not math.isfinite(duration)
            or not isinstance(ids, list)
            or any(not _opaque(value) for value in ids)
        ):
            return None
    return deepcopy(record)


def _rows(previous, current, key, *, replace_optional=()):
    previous = previous if isinstance(previous, list) else []
    old = {row.get(key): row for row in previous if isinstance(row, dict) and isinstance(row.get(key), str)}
    result = []
    for row in current:
        preserved = deepcopy(old.get(row.get(key), {}))
        for field in replace_optional:
            preserved.pop(field, None)
        result.append(dict(preserved, **row))
    return result


def record_capture(directory, session):
    """Owner's stopped-session facts, before publication and outside PCM locks.

    Proposed R65 placement: recordingDetails.captureRecord. This follows the
    existing x-schema mapping; no extra sidecar or private voice/match data.
    """
    record = from_session(session)
    if record is None or session.get("state") not in ("completed", "interrupted"):
        return False
    with metadata_transaction(directory):
        current = read_metadata(directory)
        details = deepcopy(current.get("recordingDetails", {}))
        previous = details.get("captureRecord", {})
        if (
            not isinstance(previous, dict)
            or type(previous.get("version", 1)) is not int
            or previous.get("version", 1) != 1
        ):
            return False  # Preserve a future writer's record in full.
        record["sources"] = _rows(
            previous.get("sources", []), record["sources"], "sourceTrackId", replace_optional=("captureExpected",)
        )
        record["tracks"] = _rows(previous.get("tracks", []), record["tracks"], "id")
        record = dict(previous, **record)
        if previous == record:
            return False
        details["captureRecord"] = record
        update_metadata(directory, recordingDetails=details)
    return True
