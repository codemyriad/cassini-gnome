# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only Library model. No GTK, storage writes, import or capture control.

HARMONY §§3–4/6 and DECISIONS-UX D2/D3 define derived fields. Capture activity
is injected, never guessed from timestamps. Missing offsets remain None in the
source model; zero is used only when calculating display duration/history.
"""

import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from . import portable
from .meeting_state import MeetingError, MeetingState
from .recording_session import strict_json, wave_info
from .transcript import build_turns

MAX_JSON = 1024 * 1024


def library_root(root=None):
    return Path(root if root is not None else os.environ.get("CASSINI_LIBRARY_DIR", Path.home() / "Music/Cassini"))


def meeting_id(path):
    for prefix in ("Recording-", "Received-"):
        if path.name.startswith(prefix):
            try:
                return str(UUID(path.name[len(prefix) :]))
            except ValueError:
                return None
    return None


def _safe_file(directory, name):
    if not isinstance(name, str) or Path(name).name != name or name in ("", ".", "..") or "\\" in name:
        raise ValueError("unsafe-reference")
    target = directory / name
    if target.resolve().parent != directory.resolve():
        raise ValueError("unsafe-reference")
    return target


def _json(path, *, limit=MAX_JSON):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("metadata-too-large")
    value = strict_json(data)
    if not isinstance(value, dict):
        raise ValueError("metadata-not-object")
    return value


def _integer(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def _offset(value):
    return type(value) in (int, float) and 0 <= value <= 86400000 and math.isfinite(value)


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        return math.floor(date.astimezone(UTC).timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class Diagnostic:
    path: Path
    code: str


@dataclass(frozen=True)
class CaptureTrack:
    path: Path
    frames: int
    sample_rate: int
    start_offset_ms: int | float | None
    ended: bool | None

    @property
    def display_end_ms(self):
        return math.floor((self.start_offset_ms or 0) + self.frames * 1000 / self.sample_rate)


@dataclass(frozen=True)
class LibrarySpeaker:
    id: str
    name: str
    provenance: str | None
    first_spoken_ms: int | None


@dataclass(frozen=True, kw_only=True)
class Meeting(MeetingState):
    id: str
    directory: Path
    title: str
    started_at_ms: int | None
    duration_ms: int | None
    speakers: tuple[LibrarySpeaker, ...] = ()
    quote_line: str | None = None
    tracks: tuple[CaptureTrack, ...] = ()
    audio_file: Path | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    metadata: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def speaker_names(self):
        return tuple(s.name for s in self.speakers)


@dataclass(frozen=True)
class LibrarySnapshot:
    meetings: tuple[Meeting, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    unavailable: bool = False


class LibraryScanner:
    """Keep only rebuildable in-memory Opus header/duration caches between scans."""

    def __init__(self, root=None):
        self.root = library_root(root)
        self._opus_cache = {}

    def _opus(self, path):
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = self._opus_cache.get(path)
        if cached and cached[0] == key:
            return cached[1:]
        embedded = portable.read_meeting(str(path))
        try:
            with path.open("rb") as audio:
                duration = portable.compute_audio_digest(audio)["durationMs"]
        except ValueError:
            duration = None  # Keep readable embedded words/title even if audio is incomplete.
        self._opus_cache[path] = (key, embedded, duration)
        return embedded, duration

    def scan(self, *, capture_activity=None):
        activity = dict(capture_activity or {})
        try:
            children = sorted(self.root.iterdir())
        except FileNotFoundError:
            return LibrarySnapshot()
        except OSError:
            return LibrarySnapshot(diagnostics=(Diagnostic(self.root, "library-unreadable"),), unavailable=True)
        meetings, diagnostics, used_audio = [], [], set()
        for directory in children:
            identifier = meeting_id(directory)
            if identifier is None:
                continue
            if directory.is_symlink():
                diagnostics.append(Diagnostic(directory, "unsafe-meeting-directory"))
                continue
            if not directory.is_dir():
                continue
            meeting = self._read(directory, identifier, activity.get(identifier))
            meetings.append(meeting)
            if meeting.audio_file:
                used_audio.add(meeting.audio_file)
        self._opus_cache = {path: value for path, value in self._opus_cache.items() if path in used_audio}
        meetings.sort(key=lambda meeting: (-(meeting.started_at_ms or 0), meeting.id))
        return LibrarySnapshot(tuple(meetings), tuple(diagnostics))

    def _metadata(self, directory, identifier, diagnostics):
        path = _safe_file(directory, "meeting.json")
        try:
            value = _json(path)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            diagnostics.append(Diagnostic(path, "meeting-metadata-unreadable"))
            return {}
        if type(value.get("version")) is not int or value["version"] != 1:
            diagnostics.append(Diagnostic(path, "meeting-version-unsupported"))
            return value  # Retained, never edited or treated as this version.
        try:
            same_id = str(UUID(value.get("id", ""))) == identifier
        except (ValueError, AttributeError, TypeError):
            same_id = False
        if not same_id:
            diagnostics.append(Diagnostic(path, "meeting-identity-mismatch"))
            return {}
        return value

    def _capture(self, directory, identifier, diagnostics):
        capture = {}
        try:
            capture = _json(_safe_file(directory, "session.json"))
            if (
                type(capture.get("version")) is not int
                or capture["version"] != 1
                or str(UUID(capture.get("id", ""))) != identifier
            ):
                raise ValueError("capture-identity-or-version")
            values = capture.get("tracks")
            if not isinstance(values, list) or len(values) > 64:
                raise ValueError("capture-tracks-invalid")
        except (OSError, ValueError, AttributeError, TypeError):
            diagnostics.append(Diagnostic(directory / "session.json", "capture-recovered"))
            capture = {"state": "interrupted"}
            values = [dict(file=p.name) for p in sorted(directory.glob("track-*.wav"))[:64]]
        tracks = []
        for value in values:
            try:
                if (
                    not isinstance(value, dict)
                    or not str(value.get("file", "")).startswith("track-")
                    or not str(value.get("file", "")).endswith(".wav")
                ):
                    raise ValueError("capture-track-invalid")
                path = _safe_file(directory, value["file"])
                info = wave_info(path, directory)
                offset = value.get("startOffsetMs")
                if offset is not None and not _offset(offset):
                    raise ValueError("capture-offset-invalid")
                if offset is None and info["frames"]:
                    diagnostics.append(Diagnostic(path, "start-offset-unknown-display-zero"))
                tracks.append(
                    CaptureTrack(
                        path,
                        info["frames"],
                        info["sampleRate"],
                        offset,
                        value.get("ended") if type(value.get("ended")) is bool else None,
                    )
                )
            except (OSError, ValueError, KeyError):
                diagnostics.append(Diagnostic(directory, "capture-track-unreadable"))
        return capture, tuple(tracks)

    def _read(self, directory, identifier, active):
        diagnostics = []
        try:
            metadata = self._metadata(directory, identifier, diagnostics)
        except ValueError:
            diagnostics.append(Diagnostic(directory / "meeting.json", "unsafe-reference"))
            metadata = {}
        usable = metadata if type(metadata.get("version")) is int and metadata["version"] == 1 else {}
        origin = "received" if directory.name.startswith("Received-") else "own"
        capture, tracks, embedded, audio_file, duration, started = {}, (), None, None, None, None
        lifecycle, interrupted = "saved", None
        if origin == "own":
            capture, tracks = self._capture(directory, identifier, diagnostics)
            duration = max((t.display_end_ms for t in tracks), default=0)
            started = _date(capture.get("createdAt"))
            if active in ("recording", "saving"):
                lifecycle = active
            unfinished = capture.get("state") != "completed" and lifecycle == "saved"
            gaps = (
                capture.get("problemKind") == "gap"
                or any(
                    isinstance(t, dict) and _integer(t.get("gapFrames")) and t["gapFrames"] > 0
                    for t in capture.get("tracks", [])
                )
                or (_integer(capture.get("gapFrames")) and capture["gapFrames"] > 0)
            )
            if capture.get("state") == "interrupted" or capture.get("source") == "recovered" or unfinished or gaps:
                interrupted = duration
            if lifecycle == "saved" and tracks and any(t.ended is False for t in tracks):
                interrupted = duration
        else:
            received = usable.get("received")
            if isinstance(received, dict):
                started = received.get("importedAtMs") if _integer(received.get("importedAtMs")) else None
                name = received.get("originalName")
            else:
                names = sorted(p.name for p in directory.glob("*.opus"))
                name = names[0] if len(names) == 1 else None
            try:
                audio_file = _safe_file(directory, name)
                embedded, duration = self._opus(audio_file)
                if duration is None:
                    diagnostics.append(Diagnostic(audio_file, "received-audio-duration-unknown"))
                embedded_at = _date(embedded.created_at)
                if embedded_at is not None:
                    started = embedded_at
            except (OSError, ValueError, TypeError):
                diagnostics.append(Diagnostic(directory, "received-audio-unreadable"))
        words, transcript, eta, error = self._transcript(directory, usable, embedded, diagnostics)
        error_value = usable.get("error")
        if error is None and isinstance(error_value, dict) and isinstance(error_value.get("id"), str):
            action = error_value.get("action")
            if action is None or isinstance(action, str):
                error = MeetingError(error_value["id"], action)
        title = usable.get("title") or capture.get("title") or (embedded.title if embedded else None)
        title = title if isinstance(title, str) else ""
        speakers = _speakers(words, usable.get("speakers"), embedded)
        quote = _quote(words, usable.get("quoteOverride"))
        return Meeting(
            id=identifier,
            directory=directory,
            lifecycle=lifecycle,
            origin=origin,
            interrupted_at_ms=interrupted,
            transcript=transcript,
            eta_minutes=eta,
            error=error,
            title=title,
            started_at_ms=started,
            duration_ms=duration,
            speakers=speakers,
            quote_line=quote,
            tracks=tracks,
            audio_file=audio_file,
            diagnostics=tuple(diagnostics),
            metadata=metadata,
        )

    def _transcript(self, directory, metadata, embedded, diagnostics):
        value = metadata.get("transcript")
        status = value.get("status", "none") if isinstance(value, dict) else "none"
        status = status if status in ("none", "queued", "running", "done", "failed", "damaged") else "none"
        reference = value.get("reference") if isinstance(value, dict) else None
        words, damaged = (), status == "damaged"
        try:
            if isinstance(reference, dict):
                target = _safe_file(directory, reference.get("file"))
                if (
                    reference.get("kind") == "sidecar"
                    and target.name == "transcript.json"
                    and reference.get("format") == portable.WORDS_FORMAT
                ):
                    body = _json(target, limit=portable.INFLATE_CEILING)
                    words = _sidecar_words(body)
                elif reference.get("kind") == "embedded":
                    source, _ = self._opus(target)
                    words = tuple(
                        portable.load_words(source, reference.get("transcriptId") or source.default_transcript_id)
                    )
                else:
                    raise ValueError("unsupported-transcript-reference")
            elif embedded and embedded.default_transcript_id:
                words = tuple(portable.load_words(embedded, embedded.default_transcript_id))
            elif status == "done":
                raise ValueError("missing-completed-transcript")
            elif embedded and embedded.state not in (
                portable.State.PLAIN_AUDIO,
                portable.State.UNVERIFIED,
                portable.State.OK,
            ):
                raise ValueError("damaged-embedded-transcript")
        except (OSError, ValueError, TypeError):
            damaged = True
            diagnostics.append(Diagnostic(directory, "transcript-unreadable"))
        if damaged:
            return words, "damaged", None, MeetingError("transcript-damaged", None)
        if (reference is not None or words) and status == "none":
            status = "done"
        eta = metadata.get("etaMinutes")
        if status in ("queued", "running") and not _integer(eta):
            diagnostics.append(Diagnostic(directory, "transcript-status-invalid"))
            status = "done" if reference is not None or words else "none"
        eta = eta if status in ("queued", "running") else None
        return words, status, eta, None


def _sidecar_words(body):
    items = body.get("items")
    if not isinstance(items, list):
        raise ValueError("invalid-transcript-items")
    words = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("speaker"), str)
            or not isinstance(item.get("text"), str)
        ):
            raise ValueError("invalid-transcript-word")
        start, end = item.get("startMs"), item.get("endMs")
        if not _integer(start) or not _integer(end) or end < start:
            raise ValueError("invalid-transcript-time")
        words.append(portable.Word(item["speaker"], start, end, item["text"], item.get("lowConfidenceSpeaker") is True))
    return tuple(words)


def _speakers(words, values, embedded):
    values = values if isinstance(values, list) else []
    metadata = {s["id"]: s for s in values if isinstance(s, dict) and isinstance(s.get("id"), str)}
    legacy = {s.id: s.label for s in embedded.speakers} if embedded else {}
    first = {}
    for index, word in enumerate(words):
        first[word.speaker] = min(first.get(word.speaker, (word.start_ms, index)), (word.start_ms, index))
    order = sorted(first, key=first.get)
    order += [
        s["id"]
        for s in sorted(
            metadata.values(), key=lambda s: s.get("colourIndex", 0) if _integer(s.get("colourIndex")) else 0
        )
        if s["id"] not in first
    ]
    if not order:
        order = list(legacy)
    result = []
    for rank, identifier in enumerate(order, 1):
        info = metadata.get(identifier, {})
        name = (
            (info.get("name") or f"Speaker {rank}")
            if identifier in metadata
            else (legacy.get(identifier) or f"Speaker {rank}")
        )
        name = name if isinstance(name, str) else f"Speaker {rank}"
        provenance = info.get("provenance")
        provenance = provenance if provenance in ("user", "recognised", "unnamed") else None
        result.append(LibrarySpeaker(identifier, name, provenance, first.get(identifier, (None,))[0]))
    return tuple(result)


def _quote(words, override):
    if not words:
        return None
    if isinstance(override, dict) and isinstance(override.get("text"), str) and override["text"].strip():
        text = override["text"].strip()
    else:
        candidates = [t for t in build_turns(list(words)) if t.start_ms < 600000]
        if not candidates:
            return None
        chosen = min(candidates, key=lambda t: (-(t.end_ms - t.start_ms), t.start_ms, t.index))
        text = chosen.text.strip()
    return f"“{text}”"


def scan_library(root=None, *, capture_activity=None):
    return LibraryScanner(root).scan(capture_activity=capture_activity)
