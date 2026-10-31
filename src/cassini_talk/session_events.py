# SPDX-License-Identifier: GPL-3.0-or-later
"""S-6 source/selection events. Unknown reader fields never become inferred events."""

import math
from dataclasses import dataclass

END_REASONS = frozenset(("user", "callEnded", "tabClosed", "pageGone", "disk", "error", "appQuit"))


def valid_event(event):
    if not isinstance(event, dict):
        return False
    at = event.get("atMs")
    if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
        return False
    if event.get("type") == "end":
        return isinstance(event.get("reason"), str) and event["reason"] in END_REASONS
    if event.get("type") == "othersSource":
        return event.get("source") in ("browser", "system", "none") and (
            event.get("reason") in ("start", "missing", "lost", "user")
            or event.get("reason") == "restored"
            and event.get("source") == "browser"
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
        if valid_event(event) and event["atMs"] >= previous:
            result.append(dict(event))
            previous = event["atMs"]
            if event["type"] == "end":
                break
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
        elif event["type"] == "othersSource":
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
    """Validate the complete append; reserve the final slot for a terminal end."""
    existing = manifest.get("events", [])
    pending = list(events)
    if not isinstance(existing, list) or len(existing) + len(pending) > 1000:
        raise ValueError("A recording supports at most 1000 events, including its reserved end")
    previous = existing[-1]["atMs"] if existing else 0
    ended = any(event.get("type") == "end" for event in existing)
    state = source_state(existing)
    for index, event in enumerate(pending):
        if ended or not valid_event(event) or event["atMs"] < previous:
            raise ValueError("Invalid or out-of-order recording source event")
        if event["type"] == "end":
            if index != len(pending) - 1 or manifest.get("state") not in ("completed", "interrupted"):
                raise ValueError("An end event requires finalized tracks and saved state")
            if any(
                event["atMs"] < track["endOffsetMs"]
                for track in manifest.get("tracks", [])
                if track.get("endOffsetMs") is not None
            ):
                raise ValueError("An end event cannot precede a finalized known track end")
            ended = True
        elif event.get("reason") == "restored" and not (state.fallback and state.others):
            raise ValueError("Browser restoration requires an active system fallback")
        previous = event["atMs"]
        state = source_state([*existing, *pending[: index + 1]])
    if not ended and len(existing) + len(pending) > 999:
        raise ValueError("A recording supports at most 1000 events, including its reserved end")
    manifest["events"] = existing + [dict(event) for event in pending]
