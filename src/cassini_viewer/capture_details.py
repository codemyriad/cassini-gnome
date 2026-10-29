# SPDX-License-Identifier: GPL-3.0-or-later
"""D28 Part A draft: bounded, read-only facts; no capture policy or event writes.

English is provisional pending design approval and the shared details.* catalog.
The report is a whitelist projection, never a dump of metadata or voice stores.
"""

import array
import codecs
import json
import math
import os
import sys
from dataclasses import dataclass

from . import ux_formats as fmt
from .library_files import regular
from .recording_session import strict_json
from .session_events import read_events

UNKNOWN = "Not recorded for this meeting"
JSON_LIMIT = 1024 * 1024
MIC_BUDGET = 512 * 1024
REASONS = {
    "start": "Selected when recording began",
    "missing": "Browser audio was unavailable",
    "lost": "The browser audio connection was lost",
    "user": "You changed what was recorded",
    "restored": "Browser audio returned",
}
ENDS = {
    "user": "Stopped by you",
    "callEnded": "The call ended",
    "tabClosed": "The tab was closed",
    "pageGone": "The meeting page did not come back",
    "disk": "Recording stopped because space ran low",
    "error": "Recording stopped because of an error",
    "appQuit": "Cassini was quit",
}


def clean(value, limit=180):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def number(value):
    return type(value) in (int, float) and 0 <= value <= 86400000 and math.isfinite(value)


@dataclass(frozen=True)
class Span:
    side: str
    source: str
    method: str
    start_ms: int | None
    end_ms: int | None
    reason: str = ""

    @property
    def time(self):
        if self.start_ms is None or self.end_ms is None:
            return "Timing not recorded"
        return f"{fmt.position(self.start_ms)}–{fmt.position(self.end_ms)}"


@dataclass(frozen=True)
class Report:
    title: str
    folder: str
    live: bool
    spans: tuple[Span, ...]
    ended: str
    health: tuple[tuple[str, str], ...]
    writing: tuple[tuple[str, str], ...]
    notes: tuple[str, ...] = ()

    def plain_text(self):
        lines = ["Cassini recording details", self.title, "", "How it was captured"]
        for side in ("Me", "The others"):
            lines.append(side)
            rows = [s for s in self.spans if s.side == side]
            lines.extend(f"  {s.source} · {s.method} · {s.time}" + (f" · {s.reason}" if s.reason else "") for s in rows)
            if not rows:
                lines.append("  No saved audio for this side")
        lines.extend(["", "How it ended", self.ended, "", "Audio health"])
        lines.extend(f"{key}: {value}" for key, value in self.health)
        lines.extend(["", "Writing down"])
        lines.extend(f"{key}: {value}" for key, value in self.writing)
        lines.extend(["", "Files", self.folder])
        if self.notes:
            lines.extend(["", *self.notes])
        return "\n".join(lines)


def _json(fd, filename, notes, *, optional=False):
    try:
        with regular(fd, filename) as (stream, info):
            if info.st_size > JSON_LIMIT:
                raise ValueError("too large")
            value = strict_json(stream.read(JSON_LIMIT + 1))
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
    except FileNotFoundError:
        if not optional:
            notes.append(f"{filename} is unavailable; some details are not recorded.")
    except (OSError, ValueError):
        notes.append(f"{filename} could not be read safely; some details are unavailable.")
    return {}


def _method(tracks, session):
    kinds = {t.kind for t in tracks}
    if kinds <= {"microphone", "local"}:
        return "Microphone", ", ".join(
            sorted(
                {clean(session.get(t.id, {}).get("label")) for t in tracks}
                - {"", "Me", "Microphone", "microphone", "Local audio"}
            )
        ) or "Microphone input"
    sources = []
    browser = [t for t in tracks if t.kind in ("remote", "tab", "media")]
    if browser:
        count = len({t.source_track_id or t.id for t in browser})
        sources.append("Browser audio")
        method = f"{count} audio stream" + ("s" if count != 1 else "")
    else:
        method = ""
    if "system" in kinds:
        sources.append("Computer sound")
    if "application" in kinds:
        sources.append("Application audio")
    if not sources:
        sources.append("Audio source not recorded")
    return " + ".join(sources), method or "Saved audio track" + ("s" if len(tracks) != 1 else "")


def _language(fd, notes):
    """Read a bounded top-level header; never decode a long word array."""
    try:
        with regular(fd, "transcript.json") as (stream, _info):
            prefix = codecs.getincrementaldecoder("utf-8")().decode(stream.read(65536), final=False)
        decoder = json.JSONDecoder()
        position = len(prefix) - len(prefix.lstrip())
        if prefix[position : position + 1] != "{":
            raise ValueError("invalid header")
        position += 1
        language = ""
        while position < len(prefix):
            while position < len(prefix) and prefix[position].isspace():
                position += 1
            if prefix[position : position + 1] == "}":
                return language
            name, position = decoder.raw_decode(prefix, position)
            while position < len(prefix) and prefix[position].isspace():
                position += 1
            if prefix[position : position + 1] != ":":
                raise ValueError("invalid header")
            position += 1
            if name == "items":
                return language
            while position < len(prefix) and prefix[position].isspace():
                position += 1
            value, position = decoder.raw_decode(prefix, position)
            if name == "language":
                language = clean(value, 40)
            while position < len(prefix) and prefix[position].isspace():
                position += 1
            if prefix[position : position + 1] == "}":
                return language
            if prefix[position : position + 1] != ",":
                raise ValueError("invalid header")
            position += 1
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, RecursionError):
        notes.append("The saved writing-down language could not be read from its metadata header.")
    return ""


def timeline(meeting, session):
    """Track evidence sets intervals; known events explain source/selection changes."""
    events = read_events(session.get("events"))
    raw_tracks = session.get("tracks", [])
    raw_tracks = raw_tracks[:64] if isinstance(raw_tracks, list) else []
    raw = {t.get("id"): t for t in raw_tracks if isinstance(t, dict) and isinstance(t.get("id"), str)}
    tracks = [t for t in meeting.tracks if t.frames > 0]
    duration = max([meeting.duration_ms or 0, *(t.display_end_ms for t in tracks)])
    points = {0, duration}
    for track in tracks:
        if track.start_offset_ms is not None:
            points.update((int(track.start_offset_ms), track.display_end_ms))
    points.update(int(e["atMs"]) for e in events if e["atMs"] <= duration)
    points = sorted(points)
    spans = []
    for side in ("Me", "The others"):
        rows = []
        candidates = [t for t in tracks if (t.kind in ("microphone", "local")) == (side == "Me")]
        for start, end in zip(points, points[1:], strict=False):
            if end <= start:
                continue
            active = [
                t
                for t in candidates
                if t.start_offset_ms is not None and int(t.start_offset_ms) <= start < t.display_end_ms
            ]
            preceding = [e for e in events if e["atMs"] <= start]
            selections = [e for e in preceding if e["type"] == "selection"]
            off = bool(selections) and not selections[-1]["me" if side == "Me" else "others"]
            reason = ""
            if active:
                source, method = _method(active, raw)
                if side == "The others":
                    changes = [e for e in preceding if e["type"] == "othersSource"]
                    if changes:
                        event = changes[-1]
                        # Do not explain browser track evidence as a system fallback.
                        matches = (event["source"] == "system" and all(t.kind == "system" for t in active)) or (
                            event["source"] == "browser" and all(t.kind in ("remote", "tab", "media") for t in active)
                        )
                        if matches:
                            reason = REASONS.get(event["reason"], "")
            elif off:
                source = "Not recorded"
                if selections[-1]["reason"] == "start":
                    method, reason = "Not selected", "Not selected when recording began"
                else:
                    method, reason = "Switched off", "You changed what was recorded"
            else:
                continue
            span = Span(side, source, method, start, end, reason)
            if rows and (rows[-1].source, rows[-1].method, rows[-1].reason, rows[-1].end_ms) == (
                source,
                method,
                reason,
                start,
            ):
                previous = rows.pop()
                span = Span(side, source, method, previous.start_ms, end, reason)
            rows.append(span)
        for track in candidates:
            if track.start_offset_ms is None:
                source, method = _method([track], raw)
                rows.append(Span(side, source, method, None, None))
        spans.extend(rows)
    return tuple(spans)


def _end(meeting, session):
    if meeting.lifecycle in ("recording", "saving"):
        return "Still recording" if meeting.lifecycle == "recording" else "Saving the recording"
    events = session.get("events")
    if isinstance(events, list):
        for event in reversed(events[:1000]):
            if isinstance(event, dict) and event.get("type") == "end" and number(event.get("atMs")):
                reason = ENDS.get(event.get("reason")) if isinstance(event.get("reason"), str) else None
                if reason:
                    return f"{reason} · {fmt.position(int(event['atMs']))}"
    return UNKNOWN


def _counter(tracks, field):
    rows = [t for t in tracks if type(t.get(field)) is int and 0 <= t[field] <= 9007199254740991]
    if not rows:
        return UNKNOWN
    frames = sum(t[field] for t in rows)
    milliseconds = sum(
        t[field] * 1000 / t["sampleRate"]
        for t in rows
        if type(t.get("sampleRate")) is int and 8000 <= t["sampleRate"] <= 192000
    )
    result = f"{frames:,} frames · {milliseconds / 1000:g} s"
    return result + (" (not all tracks report this)" if len(rows) != len(tracks) else "")


def _offset(fd, meeting):
    from .library import _wave

    remaining, total, count = MIC_BUDGET, 0, 0
    for track in meeting.tracks:
        if track.kind not in ("microphone", "local") or remaining < 2:
            continue
        try:
            with regular(fd, track.path.name) as (stream, info):
                frames, rate, channels = _wave(stream, info)
                if (rate, channels) != (track.sample_rate, track.channels):
                    continue
                stream.seek(44)
                length = min(remaining, rate * channels * 2, frames * channels * 2)
                values = array.array("h", stream.read(length - length % 2))
                if sys.byteorder != "little":
                    values.byteswap()
                remaining -= len(values) * 2
                total += sum(values)
                count += len(values)
        except (OSError, ValueError):
            continue
    if not count:
        return UNKNOWN
    mean = total / count
    if abs(mean) >= 512:
        return (
            f"{mean:+.0f} PCM16 (sampled start). Cassini now compensates in speech detection; saved audio is unchanged."
        )
    return "No large offset in the sampled microphone audio"


def read_details(meeting):
    notes = []
    try:
        fd = os.open(meeting.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return Report(
            meeting.title,
            str(meeting.directory),
            meeting.lifecycle == "recording",
            (),
            _end(meeting, {}),
            (),
            (),
            ("The meeting folder is unavailable.",),
        )
    try:
        session = _json(fd, "session.json", notes, optional=meeting.origin == "received")
        language = _language(fd, notes)
        turns = _json(fd, "speaker-turns.json", notes, optional=True)
        raw_tracks = session.get("tracks", [])
        raw_tracks = [t for t in raw_tracks[:64] if isinstance(t, dict)] if isinstance(raw_tracks, list) else []
        if not read_events(session.get("events")):
            notes.append(
                "Source-change events were not recorded. The timeline uses saved audio tracks; the reason for a source change is unknown."
            )
        health = (
            ("Gaps", _counter(raw_tracks, "gapFrames")),
            ("Dropped audio", _counter(raw_tracks, "droppedFrames")),
            ("Reported issue", clean(session.get("problemKind")) or UNKNOWN),
            ("Microphone offset", _offset(fd, meeting)),
        )
    finally:
        os.close(fd)
    stored = meeting.metadata.get("transcript") or {}
    stored = stored if isinstance(stored, dict) else {}
    tag = clean(stored.get("model"))
    model = (
        "Parakeet Ultra"
        if tag.startswith("parakeet-ultra")
        else "Whistle"
        if tag.startswith("whistle")
        else tag or UNKNOWN
    )
    recognised = sorted({clean(s.name) for s in meeting.speakers if s.provenance == "recognised"})
    separation = clean(turns.get("model"))
    if separation.startswith("nemotron-3-diarization"):
        from .worker_diarization import FORMAT, valid_group

        groups = turns.get("groups")
        if (
            turns.get("format") == FORMAT
            and isinstance(groups, list)
            and len(groups) <= 64
            and all(valid_group(g) for g in groups)
            and len({g["id"] for g in groups}) == len(groups)
        ):
            voices = {(g["id"], t["speaker"]) for g in groups for t in g["turns"]}
            separation = "Nemotron · " + str(len(voices)) + " voices"
        else:
            separation = "Nemotron · voice count unavailable"
    writing = (
        ("Model", model),
        ("Language", language if language and language != "und" else UNKNOWN),
        ("Time taken", UNKNOWN),
        ("Speech segments", UNKNOWN),
        ("Speaker separation", separation or UNKNOWN),
        ("Saved voices recognised", ", ".join(recognised) or "None recorded"),
    )
    return Report(
        clean(meeting.title, 500),
        str(meeting.directory),
        meeting.lifecycle == "recording",
        timeline(meeting, session),
        _end(meeting, session),
        health,
        writing,
        tuple(notes),
    )
