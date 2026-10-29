# SPDX-License-Identifier: GPL-3.0-or-later
"""S-6 source/selection events. Unknown reader fields never become inferred events."""

import math
from dataclasses import dataclass


def valid_event(event, *, allow_reserved=False):
    if not isinstance(event, dict):
        return False
    at = event.get("atMs")
    if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= 86400000:
        return False
    if event.get("type") == "othersSource":
        return event.get("source") in ("browser", "system", "none") and (
            event.get("reason") in ("start", "missing", "lost", "user")
            or allow_reserved
            and event.get("reason") == "restored"
        )
    if event.get("type") == "selection":
        return (
            type(event.get("me")) is bool
            and type(event.get("others")) is bool
            and (event["me"] or event["others"])
            and event.get("reason") in ("start", "user")
        )
    return False


def read_events(value):
    """Ignore unknown types/fields and malformed entries; never modify the input."""
    if not isinstance(value, (list, tuple)):
        return ()
    result, previous = [], 0
    for event in value[:1000]:
        if valid_event(event, allow_reserved=True) and event["atMs"] >= previous:
            result.append(dict(event))
            previous = event["atMs"]
    return tuple(result)


@dataclass(frozen=True)
class SourceState:
    me: bool = True
    others: bool = True
    source: str = "browser"
    reason: str | None = None
    since_ms: int | float | None = None

    @property
    def fallback(self):
        return self.source == "system" and self.reason in ("missing", "lost")


def source_state(events, *, default_source="browser", me=True, others=True):
    source, reason, since = default_source, None, None
    for event in read_events(events):
        if event["type"] == "selection":
            me, others = event["me"], event["others"]
        else:
            source, reason, since = event["source"], event["reason"], event["atMs"]
    return SourceState(me, others, source, reason, since)


def meeting_sources(meeting):
    tracks = [track for track in meeting.tracks if track.ended is not True]
    me = any(track.kind in ("microphone", "local") for track in tracks)
    others = any(track.kind not in ("microphone", "local") for track in tracks)
    return source_state(
        meeting.capture_events,
        default_source="system" if meeting.capture_source == "gnome" else "browser",
        me=me if tracks else True,
        others=others if tracks else True,
    )


def append_events(manifest, events):
    """Validate the complete append before changing a writer-owned manifest."""
    existing = manifest.get("events", [])
    if not isinstance(existing, list) or len(existing) + len(events) > 1000:
        raise ValueError("A recording supports at most 1000 source/selection events")
    previous = existing[-1]["atMs"] if existing else 0
    for event in events:
        if not valid_event(event) or event["atMs"] < previous:
            raise ValueError("Invalid or out-of-order recording source event")
        previous = event["atMs"]
    manifest["events"] = existing + [dict(event) for event in events]
