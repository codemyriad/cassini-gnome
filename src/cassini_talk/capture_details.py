# SPDX-License-Identifier: GPL-3.0-or-later
"""D28 Part A draft: bounded, read-only facts; no capture policy or event writes.

The report is a whitelist projection, never a dump of metadata or voice stores.
"""

import array
import codecs
import json
import math
import os
import sys
from dataclasses import dataclass
from fractions import Fraction

from . import ux_formats as fmt
from .library_files import regular
from .recording_session import strict_json
from .session_events import read_events
from .strings import text

UNKNOWN = text("details.unknown")
BROWSER_SOURCES = {"chromium", "chrome", "firefox", "browser", "browser-host", "safari"}
LANGUAGES = {
    "bg": text("details.language.bg"),
    "cs": text("details.language.cs"),
    "da": text("details.language.da"),
    "de": text("details.language.de"),
    "el": text("details.language.el"),
    "en": text("details.language.en"),
    "es": text("details.language.es"),
    "et": text("details.language.et"),
    "fi": text("details.language.fi"),
    "fr": text("details.language.fr"),
    "hr": text("details.language.hr"),
    "hu": text("details.language.hu"),
    "it": text("details.language.it"),
    "lt": text("details.language.lt"),
    "lv": text("details.language.lv"),
    "mt": text("details.language.mt"),
    "nb": text("details.language.nb"),
    "nl": text("details.language.nl"),
    "nn": text("details.language.nn"),
    "no": text("details.language.no"),
    "pl": text("details.language.pl"),
    "pt": text("details.language.pt"),
    "ro": text("details.language.ro"),
    "ru": text("details.language.ru"),
    "sk": text("details.language.sk"),
    "sl": text("details.language.sl"),
    "sv": text("details.language.sv"),
    "uk": text("details.language.uk"),
}
JSON_LIMIT = 1024 * 1024
MIC_BUDGET = 512 * 1024
REASONS = {
    "start": text("details.reason.start"),
    "missing": text("details.reason.missing"),
    "lost": text("details.reason.lost"),
    "user": text("details.reason.user"),
    "restored": text("details.reason.restored"),
}
ENDS = {
    "user": text("details.end.user"),
    "callEnded": text("details.end.callEnded"),
    "tabClosed": text("details.end.tabClosed"),
    "pageGone": text("details.end.pageGone"),
    "disk": text("details.end.disk"),
    "error": text("details.end.error"),
    "appQuit": text("details.end.appQuit"),
}


def side_title(side):
    return text("details.side.me" if side == "Me" else "details.side.others")


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
            return text("details.timingUnknown")
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
        lines = [text("details.copy.heading"), self.title, "", text("details.capture.title")]
        for side in ("Me", "The others"):
            lines.append(side_title(side))
            rows = [s for s in self.spans if s.side == side]
            lines.extend(f"  {s.source} · {s.method} · {s.time}" + (f" · {s.reason}" if s.reason else "") for s in rows)
            if not rows:
                lines.append("  " + text("details.copy.sideEmpty"))
        lines.extend(["", text("details.end.title"), self.ended, "", text("details.health.title")])
        lines.extend(f"{key}: {value}" for key, value in self.health)
        lines.extend(["", text("details.writing.title")])
        lines.extend(f"{key}: {value}" for key, value in self.writing)
        lines.extend(["", text("details.files.title"), self.folder])
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
            notes.append(text("details.metadata.missing", filename=filename))
    except (OSError, ValueError):
        notes.append(text("details.metadata.unsafe", filename=filename))
    return {}


def _method(tracks, session, capture_source):
    kinds = {t.kind for t in tracks}
    if kinds <= {"microphone", "local"}:
        return text("details.source.microphone"), ", ".join(
            sorted(
                {clean(session.get(t.id, {}).get("label")) for t in tracks}
                - {"", "Me", "Microphone", "microphone", "Local audio"}
            )
        ) or text("details.method.microphone")
    sources = []
    browser_media = capture_source in BROWSER_SOURCES
    browser = [t for t in tracks if t.kind in ("remote", "tab") or t.kind == "media" and browser_media]
    if browser:
        count = len({t.source_track_id or t.id for t in browser})
        sources.append(text("details.source.browser"))
        method = text("details.method.stream.one" if count == 1 else "details.method.stream.other", n=count)
    else:
        method = ""
    if "system" in kinds:
        sources.append(text("details.source.system"))
    if "application" in kinds:
        sources.append(text("details.source.application"))
    if not sources:
        sources.append(text("details.source.unknown"))
    return " + ".join(sources), method or text(
        "details.method.track.one" if len(tracks) == 1 else "details.method.track.other"
    )


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
        notes.append(text("details.metadata.languageReadFailed"))
    return ""


@dataclass(frozen=True)
class _CapturedSpan:
    id: str
    kind: str
    frames: int
    sample_rate: int
    start_offset_ms: int | float | None
    source_track_id: str | None

    @property
    def display_end_ms(self):
        return math.floor(Fraction(str(self.start_offset_ms or 0)) + Fraction(self.frames * 1000, self.sample_rate))


def _captured_spans(record):
    """Display-only facts, never audio paths or an invitation to open a file."""
    from .recording_session import KINDS

    rows = record.get("tracks", [])
    if not isinstance(rows, list):
        return ()
    spans = []
    for row in rows[:64]:
        if not isinstance(row, dict):
            continue
        identifier, kind, frames, rate, offset = (
            row.get(key) for key in ("id", "kind", "frames", "sampleRate", "startOffsetMs")
        )
        if (
            not isinstance(identifier, str)
            or not 0 < len(identifier) <= 200
            or not isinstance(kind, str)
            or kind not in KINDS
            or type(frames) is not int
            or not 0 < frames <= 9007199254740991
            or type(rate) is not int
            or not 8000 <= rate <= 192000
            or offset is not None
            and not number(offset)
        ):
            continue
        identity = row.get("sourceTrackId")
        identity = identity if isinstance(identity, str) else None
        spans.append(_CapturedSpan(identifier, kind, frames, rate, offset, identity))
    return tuple(spans)


def timeline(meeting, session, *, tracks=None):
    """Track evidence sets intervals; known events explain source/selection changes."""
    events = read_events(session.get("events"))
    raw_tracks = session.get("tracks", [])
    raw_tracks = raw_tracks[:64] if isinstance(raw_tracks, list) else []
    raw = {t.get("id"): t for t in raw_tracks if isinstance(t, dict) and isinstance(t.get("id"), str)}
    tracks = [t for t in (meeting.tracks if tracks is None else tracks) if t.frames > 0]
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
                source = text("details.source.none")
                if selections[-1]["reason"] == "start":
                    method, reason = text("details.method.notSelected"), text("details.reason.notSelected")
                else:
                    method, reason = text("details.method.off"), text("details.reason.user")
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
        return text("details.end.recording" if meeting.lifecycle == "recording" else "details.end.saving")
    events = read_events(session.get("events"))
    if events and events[-1]["type"] == "end":
        event = events[-1]
        return text("details.end.at", reason=ENDS[event["reason"]], time=fmt.position(int(event["atMs"])))
    details = meeting.metadata.get("recordingDetails") or {}
    end = details.get("end") if isinstance(details, dict) else None
    if isinstance(end, dict) and isinstance(end.get("reason"), str) and end["reason"] in ENDS:
        result = ENDS[end["reason"]]
        at = end.get("atMs")
        if type(at) is int and 0 <= at <= 9007199254740991:
            result = text("details.end.at", reason=result, time=fmt.position(at))
        return result
    return UNKNOWN


def _counter(tracks, field):
    rows = [t for t in tracks if type(t.get(field)) is int and 0 <= t[field] <= 9007199254740991]
    if not rows:
        return UNKNOWN
    frames = sum(t[field] for t in rows)
    if all(type(t.get("sampleRate")) is int and 8000 <= t["sampleRate"] <= 192000 for t in rows):
        milliseconds = sum(t[field] * 1000 / t["sampleRate"] for t in rows)
        duration = text("details.duration.seconds", n=f"{milliseconds / 1000:g}")
    else:
        duration = text("details.health.durationUnknown")
    result = text(
        "details.health.counter.one" if frames == 1 else "details.health.counter.other",
        frames=f"{frames:,}",
        duration=duration,
    )
    return text("details.health.partialCounter", value=result) if len(rows) != len(tracks) else result


def _offset(fd, meeting):
    from .library import _wave

    remaining, total, count = MIC_BUDGET, 0, 0
    for track in meeting.tracks:
        # A compacted Opus span (S-12 §5) has no raw PCM DC level to measure.
        if track.kind not in ("microphone", "local") or remaining < 2 or track.codec != "pcm":
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
        return text("details.health.largeOffset", offset=f"{mean:+.0f}")
    return text("details.health.offsetFine")


def _wall_time(value):
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        return UNKNOWN
    minutes, remainder = divmod(value, 60000)
    hours, minutes = divmod(minutes, 60)
    parts = [text("details.duration.hours", n=hours)] if hours else []
    if minutes:
        parts.append(text("details.duration.minutes", n=minutes))
    parts.append(text("details.duration.seconds", n=f"{remainder / 1000:g}"))
    return " ".join(parts)


def _separation(value):
    if not isinstance(value, dict):
        return UNKNOWN
    status = value.get("status")
    reasons = {
        key: text("details.separation." + key)
        for key in (
            "modelsNotInstalled",
            "modelNotReady",
            "unavailable",
            "notApplicable",
            "tooShort",
            "processingFailed",
        )
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
    name = text("details.model.nemotron") if model == "nemotron-3-diarization-int8" else model
    result = text(
        "details.separation.voices.one" if count == 1 else "details.separation.voices.other", model=name, n=count
    )
    result += f"\n{model}@{revision}"
    if value.get("reused") is True:
        result += "\n" + text("details.separation.reused")
    if status == "partial":
        result += "\n" + text("details.separation.partial")
    return result


def read_details(meeting):
    from .capture_report import stored as stored_capture

    capture_record = stored_capture(meeting.metadata)
    folder = str(meeting.audio_file.parent if meeting.audio_file else meeting.directory)
    notes = []
    try:
        fd = os.open(meeting.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        if capture_record is None:
            return Report(
                meeting.title,
                folder,
                meeting.lifecycle == "recording",
                (),
                _end(meeting, {}),
                (),
                (),
                (text("details.folderUnavailable"),),
            )
        fd = None  # A committed file is readable without rebuilding its cache.
    try:
        session = (
            _json(fd, "session.json", notes, optional=meeting.origin == "received" or capture_record is not None)
            if fd is not None
            else {}
        )
        # The committed file carries these facts after the WAV/session cache is
        # removed. Live session facts remain authoritative when they exist.
        if not session and capture_record is not None:
            session = {key: capture_record[key] for key in ("tracks", "events", "problemKind") if key in capture_record}
        language = _language(fd, notes) if fd is not None else ""
        raw_tracks = session.get("tracks", [])
        raw_tracks = [t for t in raw_tracks[:64] if isinstance(t, dict)] if isinstance(raw_tracks, list) else []
        if not any(event["type"] in ("othersSource", "selection") for event in read_events(session.get("events"))):
            notes.append(text("details.timelineUnknown"))
        health = []
        for title, field in (
            (text("details.health.gaps"), "gapFrames"),
            (text("details.health.dropped"), "droppedFrames"),
        ):
            if meeting.capture_source != "gnome" or any(field in t for t in raw_tracks):
                health.append((title, _counter(raw_tracks, field)))
        problem = clean(session.get("problemKind"))
        if problem or meeting.capture_source != "gnome":
            health.append((text("details.health.problem"), problem or UNKNOWN))
        health.append((text("details.health.offset"), _offset(fd, meeting) if fd is not None else UNKNOWN))
        from .output_mute import intervals

        if intervals(meeting.metadata):
            health.append((text("recordFirst.sound.title"), text("details.health.outputMuted")))
        health = tuple(health)
    finally:
        if fd is not None:
            os.close(fd)
    stored = meeting.metadata.get("transcript") or {}
    stored = stored if isinstance(stored, dict) else {}
    tag = clean(stored.get("model"))
    model = (
        text("details.model.parakeet")
        if tag.startswith("parakeet-ultra")
        else text("details.model.whistle")
        if tag.startswith("whistle")
        else tag or UNKNOWN
    )
    recognised = sorted({clean(s.name) for s in meeting.speakers if s.provenance == "recognised"})
    details = meeting.metadata.get("recordingDetails") or {}
    writing_facts = details.get("writingDown") if isinstance(details, dict) else None
    writing_facts = writing_facts if isinstance(writing_facts, dict) and meeting.transcript == "done" else {}
    wall_time = _wall_time(writing_facts.get("wallDurationMs"))
    if wall_time != UNKNOWN and writing_facts.get("resumed") is True:
        wall_time = text("details.writing.resumed", duration=wall_time)
    segments = writing_facts.get("speechSegments")
    segments = str(segments) if type(segments) is int and 0 <= segments <= 9007199254740991 else UNKNOWN
    separation = _separation(writing_facts.get("speakerSeparation"))
    language = LANGUAGES.get(language.lower().replace("_", "-").split("-", 1)[0], UNKNOWN)
    writing = (
        (text("details.writing.model"), model),
        (text("details.writing.language"), language),
        (text("details.writing.timeTaken"), wall_time),
        (text("details.writing.speechSegments"), segments),
        (text("details.writing.separation"), separation),
        (text("details.writing.recognised"), ", ".join(recognised) or text("details.writing.noneRecognised")),
    )
    if meeting.lifecycle in ("recording", "saving"):
        writing = tuple((name, text("details.writing.after")) for name, _value in writing)
    return Report(
        clean(meeting.title, 500),
        folder,
        meeting.lifecycle == "recording",
        timeline(
            meeting, session, tracks=_captured_spans(capture_record) if not meeting.tracks and capture_record else None
        ),
        _end(meeting, session),
        health,
        writing,
        tuple(notes),
    )
