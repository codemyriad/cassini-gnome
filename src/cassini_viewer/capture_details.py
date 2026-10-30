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
BROWSER_SOURCES = {"chromium", "chrome", "firefox", "browser", "browser-host", "safari"}
LANGUAGES = {
    "bg": "Bulgarian",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "fi": "Finnish",
    "fr": "French",
    "hr": "Croatian",
    "hu": "Hungarian",
    "it": "Italian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mt": "Maltese",
    "nb": "Norwegian Bokmål",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sv": "Swedish",
    "uk": "Ukrainian",
}
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


def _method(tracks, session, capture_source):
    kinds = {t.kind for t in tracks}
    if kinds <= {"microphone", "local"}:
        return "Microphone", ", ".join(
            sorted(
                {clean(session.get(t.id, {}).get("label")) for t in tracks}
                - {"", "Me", "Microphone", "microphone", "Local audio"}
            )
        ) or "Microphone input"
    sources = []
    browser_media = capture_source in BROWSER_SOURCES
    browser = [t for t in tracks if t.kind in ("remote", "tab") or t.kind == "media" and browser_media]
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
                source, method = _method(active, raw, meeting.capture_source)
                if side == "The others":
                    changes = [e for e in preceding if e["type"] == "othersSource"]
                    if changes:
                        event = changes[-1]
                        # Do not explain browser track evidence as a system fallback.
                        matches = (event["source"] == "system" and all(t.kind == "system" for t in active)) or (
                            event["source"] == "browser" and all(t.kind in ("remote", "tab", "media") for t in active)
                        )
                        browser_reason = event["reason"] in ("missing", "lost", "restored")
                        if matches and (not browser_reason or meeting.capture_source in BROWSER_SOURCES):
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
                source, method = _method([track], raw, meeting.capture_source)
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
    details = meeting.metadata.get("recordingDetails") or {}
    end = details.get("end") if isinstance(details, dict) else None
    if isinstance(end, dict) and isinstance(end.get("reason"), str) and end["reason"] in ENDS:
        result = ENDS[end["reason"]]
        at = end.get("atMs")
        if type(at) is int and 0 <= at <= 9007199254740991:
            result += " · " + fmt.position(at)
        return result
    return UNKNOWN


def _counter(tracks, field):
    rows = [t for t in tracks if type(t.get(field)) is int and 0 <= t[field] <= 9007199254740991]
    if not rows:
        return UNKNOWN
    frames = sum(t[field] for t in rows)
    if all(type(t.get("sampleRate")) is int and 8000 <= t["sampleRate"] <= 192000 for t in rows):
        milliseconds = sum(t[field] * 1000 / t["sampleRate"] for t in rows)
        duration = f"{milliseconds / 1000:g} s"
    else:
        duration = "duration not recorded"
    result = f"{frames:,} frames · {duration}"
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
                data = stream.read(length - length % 2)
                remaining -= len(data)
                values = array.array("h", data[: len(data) - len(data) % 2])
                if sys.byteorder != "little":
                    values.byteswap()
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


def _wall_time(value):
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        return UNKNOWN
    minutes, remainder = divmod(value, 60000)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours} h"] if hours else []
    if minutes:
        parts.append(f"{minutes} min")
    parts.append(f"{remainder / 1000:g} s")
    return " ".join(parts)


def _separation(value):
    if not isinstance(value, dict):
        return UNKNOWN
    status = value.get("status")
    reasons = {
        "modelsNotInstalled": "Tell speakers apart was not installed",
        "modelNotReady": "Tell speakers apart was not ready",
        "unavailable": "Tell speakers apart was unavailable on this computer",
        "notApplicable": "Not needed for these separate audio tracks",
        "tooShort": "The audio was too short to separate speakers",
        "processingFailed": "Speaker separation could not finish. The transcript was kept.",
    }
    if status in ("skipped", "failed"):
        return reasons.get(value.get("reason"), UNKNOWN)
    count = value.get("speakersFound")
    model, revision = clean(value.get("modelId")), clean(value.get("modelRevision"))
    if (
        status not in ("completed", "partial")
        or type(count) is not int
        or not 0 <= count <= 1000
        or not model
        or not revision
    ):
        return UNKNOWN
    name = "Nemotron" if model == "nemotron-3-diarization-int8" else model
    result = f"{name} · {count} " + ("voice" if count == 1 else "voices")
    result += f"\n{model}@{revision}"
    if value.get("reused") is True:
        result += "\nSaved speaker turns reused"
    if status == "partial":
        result += "\nSome audio could not be separated"
    return result


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
        raw_tracks = session.get("tracks", [])
        raw_tracks = [t for t in raw_tracks[:64] if isinstance(t, dict)] if isinstance(raw_tracks, list) else []
        if not read_events(session.get("events")):
            notes.append(
                "Source-change events were not recorded. The timeline uses saved audio tracks; the reason for a source change is unknown."
            )
        health = []
        for title, field in (("Gaps", "gapFrames"), ("Dropped audio", "droppedFrames")):
            if meeting.capture_source != "gnome" or any(field in t for t in raw_tracks):
                health.append((title, _counter(raw_tracks, field)))
        problem = clean(session.get("problemKind"))
        if problem or meeting.capture_source != "gnome":
            health.append(("Reported issue", problem or UNKNOWN))
        health.append(("Microphone offset", _offset(fd, meeting)))
        health = tuple(health)
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
    details = meeting.metadata.get("recordingDetails") or {}
    writing_facts = details.get("writingDown") if isinstance(details, dict) else None
    writing_facts = writing_facts if isinstance(writing_facts, dict) and meeting.transcript == "done" else {}
    wall_time = _wall_time(writing_facts.get("wallDurationMs"))
    if wall_time != UNKNOWN and writing_facts.get("resumed") is True:
        wall_time += " (resumed; this run)"
    segments = writing_facts.get("speechSegments")
    segments = str(segments) if type(segments) is int and 0 <= segments <= 9007199254740991 else UNKNOWN
    separation = _separation(writing_facts.get("speakerSeparation"))
    language = LANGUAGES.get(language.lower().replace("_", "-").split("-", 1)[0], UNKNOWN)
    writing = (
        ("Model", model),
        ("Language", language),
        ("Time taken", wall_time),
        ("Speech segments", segments),
        ("Speaker separation", separation),
        ("Saved voices recognised", ", ".join(recognised) or "None recorded"),
    )
    if meeting.lifecycle in ("recording", "saving"):
        writing = tuple((name, "After the recording") for name, _value in writing)
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
