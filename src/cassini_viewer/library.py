# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only Library derivation, D6 R1–R10. No GTK, repairs or metadata writes."""

import math
import os
import re
import stat
import struct
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from . import portable, ux_formats
from .library_files import FileCache, basename, key
from .library_schema import valid
from .meeting_state import MeetingError, MeetingState
from .meeting_text import reading_turns
from .recording_session import KINDS, strict_json
from .session_events import meeting_sources, read_events
from .strings import text
from .transcript import build_turns, visible_turns

MAX_JSON = 1024 * 1024


def library_root(root=None):
    from .library_location import primary

    return Path(root) if root is not None else primary()


def meeting_id(path):
    for prefix in ("Recording-", "Received-"):
        if path.name.startswith(prefix):
            suffix = path.name[len(prefix) :]
            try:
                return suffix if str(UUID(suffix)) == suffix else None
            except ValueError:
                return None
    return None


def _integer(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return math.floor(date.astimezone(UTC).timestamp() * 1000) if date.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def _json(stream, _info, limit=MAX_JSON):
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("metadata-too-large")
    value = strict_json(data)
    if not isinstance(value, dict):
        raise ValueError("metadata-not-object")
    return value


def _wave(stream, info):
    header = stream.read(44)
    if len(header) != 44:
        raise ValueError("incomplete-wav-header")
    riff, _, wave, fmt, size, encoding, channels, rate, byte_rate, alignment, bits, data, _ = struct.unpack(
        "<4sI4s4sIHHIIHH4sI", header
    )
    if (
        riff != b"RIFF"
        or wave != b"WAVE"
        or fmt != b"fmt "
        or data != b"data"
        or size != 16
        or encoding != 1
        or bits != 16
        or channels not in (1, 2)
        or not 8000 <= rate <= 192000
        or alignment != channels * 2
        or byte_rate != rate * channels * 2
    ):
        raise ValueError("unsupported-pcm-header")
    return (info.st_size - 44) // alignment, rate, channels


def _opus_duration(stream, head, info):
    """Read the final complete EOS page (at most 65307 bytes), not the whole audio."""
    if len(head) < 19:
        return None
    stream.seek(0)
    first = stream.read(27)
    serial = first[14:18]
    stream.seek(max(0, info.st_size - 65536))
    tail = stream.read(65536)
    position = len(tail)
    while (position := tail.rfind(b"OggS", 0, position)) >= 0:
        page = tail[position:]
        if len(page) < 27 or page[4] != 0 or not page[5] & 4 or page[14:18] != serial:
            continue
        segments = page[26]
        if len(page) < 27 + segments or 27 + segments + sum(page[27 : 27 + segments]) != len(page):
            continue
        checksum = struct.unpack_from("<I", page, 22)[0]
        if portable.ogg_crc(page[:22] + bytes(4) + page[26:]) != checksum:
            continue
        (granule,) = struct.unpack_from("<Q", page, 6)
        (pre_skip,) = struct.unpack_from("<H", head, 10)
        if pre_skip <= granule <= 0x7FFFFFFFFFFFFFFF:
            return (granule - pre_skip) * 1000 // 48000
    return None


def _opus(stream, info):
    head, comments, vendor = portable.read_header(stream)
    embedded = portable.interpret(head, comments, vendor)
    duration = _opus_duration(stream, head, info)
    trusted = embedded.state == portable.State.PLAIN_AUDIO
    if embedded.is_readable and embedded.manifest is not None:
        integrity = embedded.manifest.get("integrity", {})
        try:
            actual = portable.compute_audio_digest(stream)
            trusted = (
                integrity.get("matchPolicy") == "exact-opus-audio-v1"
                and integrity.get("opusAudioSha256") == actual["sha256"]
                and all(integrity.get(k) == actual[k] for k in ("channels", "sampleCount", "durationMs"))
                and integrity.get("sampleRate") == 48000
            )
        except ValueError:
            trusted = False
    return embedded, duration, trusted, {}, info.st_size


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
    channels: int = 1
    id: str | None = None
    kind: str = "unknown"
    source_track_id: str | None = None

    @property
    def display_end_ms(self):
        # Keep source offsets untouched; only display positions are integer ms.
        total = Fraction(str(self.start_offset_ms or 0)) + Fraction(self.frames * 1000, self.sample_rate)
        return total.numerator // total.denominator


@dataclass(frozen=True)
class LibrarySpeaker:
    id: str
    name: str
    provenance: str | None
    first_spoken_ms: int
    colour_index: int
    talk_ms: int = 0
    sample_span: tuple[int, int] | None = None


@dataclass(frozen=True)
class PreviewTurn:
    speaker: str
    start_ms: int
    text: str


@dataclass(frozen=True, kw_only=True)
class Meeting(MeetingState):
    id: str
    directory: Path
    title: str
    started_at_ms: int | None
    duration_ms: int | None
    speakers: tuple[LibrarySpeaker, ...] = ()
    words: tuple[portable.Word, ...] = ()
    preview_turns: tuple[PreviewTurn, ...] = ()
    turns: tuple = field(default=(), compare=False, repr=False)
    quote_line: str | None = None
    tracks: tuple[CaptureTrack, ...] = ()
    capture_source: str | None = None
    capture_state: str | None = None
    capture_events: tuple[dict, ...] = ()
    audio_file: Path | None = None
    audio_size_bytes: int | None = None
    legacy_transcript_file: Path | None = None
    has_audio: bool = False
    metadata_editable: bool = True
    diagnostics: tuple[Diagnostic, ...] = ()
    metadata: dict = field(default_factory=dict, compare=False, repr=False)
    notes: tuple[dict, ...] = ()
    extensions: dict = field(default_factory=dict, compare=False, repr=False)

    collection_key: str | None = None

    @property
    def key(self):
        return self.collection_key or self.directory.name

    @property
    def speaker_names(self):
        return tuple(s.name for s in self.speakers)

    def row_texts(self):
        """R11 state-specific meta and second line; no capture labels become names."""
        if self.lifecycle == "recording":
            state = meeting_sources(self)
            if self.capture_source in ("chromium", "chrome", "firefox", "browser", "browser-host") and (
                not self.capture_events or (state.others and state.source == "browser")
            ):
                return text("common.listeningInsideBrowser"), text("library.row.recording.line")
            return None, None
        if self.lifecycle == "saving":
            return None, None
        if self.transcript in ("queued", "running"):
            return None, text(
                "library.row.transcribing.line", duration=ux_formats.meeting_duration(self.duration_ms)
            ) if self.duration_ms is not None else None
        return ux_formats.row_meta(self.duration_ms, self.speaker_names), self.quote_line

    @property
    def can_write_down(self):
        return super().can_write_down and self.has_audio and self.metadata_editable


@dataclass(frozen=True)
class LibraryGroup:
    title: str
    meetings: tuple[Meeting, ...]


@dataclass(frozen=True)
class LibrarySnapshot:
    meetings: tuple[Meeting, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    unavailable: bool = False
    directories: tuple[Path, ...] = ()

    def groups(self, *, now_ms, tz):
        result = []
        for meeting in self.meetings:
            title = ux_formats.day_group(meeting.started_at_ms, now_ms=now_ms, tz=tz)
            if result and result[-1].title == title:
                previous = result[-1]
                result[-1] = LibraryGroup(title, previous.meetings + (meeting,))
            else:
                result.append(LibraryGroup(title, (meeting,)))
        return tuple(result)

    def as_dict(self, *, now_ms, tz):
        groups = []
        for group in self.groups(now_ms=now_ms, tz=tz):
            rows = []
            for meeting in group.meetings:
                elapsed = max(0, now_ms - meeting.started_at_ms) if meeting.started_at_ms is not None else 0
                badge = meeting.badge(live_ms=elapsed)
                rows.append(
                    dict(
                        id=meeting.id,
                        origin=meeting.origin,
                        lifecycle=meeting.lifecycle,
                        interruptedAtMs=meeting.interrupted_at_ms,
                        transcript=meeting.transcript,
                        etaMinutes=meeting.eta_minutes,
                        error=dict(id=meeting.error.id, action=meeting.error.action) if meeting.error else None,
                        badge=badge.text if badge else None,
                        title=meeting.title,
                        startMs=meeting.started_at_ms,
                        durationMs=meeting.duration_ms,
                        names=list(meeting.speaker_names),
                        quote=meeting.quote_line,
                        canWriteDown=meeting.can_write_down,
                        speakers=[dict(id=s.id, name=s.name, colourIndex=s.colour_index) for s in meeting.speakers],
                    )
                )
            groups.append(dict(title=group.title, meetings=rows))
        return dict(scan="failed" if self.unavailable else "ok" if self.meetings else "empty", groups=groups)


def _sort(meeting):
    unknown = meeting.started_at_ms is None
    return (
        unknown,
        -(meeting.started_at_ms or 0) if not unknown else 0,
        meeting.title.casefold() if unknown else "",
        meeting.title if unknown else "",
        meeting.id,
        meeting.key,
    )


class LibraryScanner:
    """Reusable scanner with incremental rows and per-file stat-keyed caches.

    scan(changed_directories=...) updates only those direct children; None lists
    the root. One lock serializes scans/caches, including worker-thread callers.
    """

    def __init__(self, root=None):
        self.root = library_root(root)
        self._lock = threading.RLock()
        self._files = FileCache()
        self._rows = {}
        self._row_cache = {}
        self._folder_diagnostics = {}
        self._directories = {}
        self._preview_cache = {}

    def scan(self, *, capture_activity=None, changed_directories=None):
        with self._lock:
            return self._scan(capture_activity or {}, changed_directories)

    def _scan(self, activity, changed):
        activity = {str(k).lower(): v for k, v in activity.items()}
        diagnostics = []
        try:
            try:
                os.stat(self.root)
            except FileNotFoundError:
                try:
                    os.lstat(self.root)
                except FileNotFoundError:
                    for name in list(self._row_cache):
                        self._forget(name)
                    self._rows.clear()
                    self._directories.clear()
                    self._folder_diagnostics.clear()
                    self._files.entries.clear()
                    self._preview_cache.clear()
                    return LibrarySnapshot()
                raise OSError("dangling-root") from None
            resolved = self.root.resolve()
            root_fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except (OSError, RuntimeError):
            return LibrarySnapshot(diagnostics=(Diagnostic(self.root, "library-unreadable"),), unavailable=True)
        try:
            if changed is None:
                names = set(os.listdir(root_fd))
                for old in (
                    self._row_cache.keys() | self._folder_diagnostics.keys() | self._directories.keys()
                ) - names:
                    self._forget(old)
            else:
                names = {Path(p).name for p in changed}
            for name in sorted(names):
                directory = resolved / name
                identifier = meeting_id(directory)
                if identifier is None:
                    continue
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    self._forget(name)
                    continue
                except Exception:
                    self._forget(name)
                    self._folder_diagnostics[name] = Diagnostic(directory, "meeting-unreadable")
                    meeting = _unreadable(directory, identifier, activity.get(identifier))
                    if meeting is not None:
                        self._rows[name] = meeting
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    self._forget(name)
                    if stat.S_ISLNK(info.st_mode):
                        self._folder_diagnostics[name] = Diagnostic(directory, "unsafe-meeting-directory")
                    continue
                self._folder_diagnostics.pop(name, None)
                self._directories[name] = directory
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=root_fd)
                    try:
                        signature = []
                        for child in os.listdir(fd):
                            if child in ("session.json", "meeting.json", "transcript.json") or child.endswith(
                                (".opus", ".wav")
                            ):
                                try:
                                    item = os.stat(child, dir_fd=fd, follow_symlinks=False)
                                    signature.append((child, item.st_mode, key(item)))
                                except FileNotFoundError:
                                    continue
                        fingerprint = (key(info)[:2], tuple(sorted(signature)), activity.get(identifier))
                        cached = self._row_cache.get(name)
                        if cached and cached[0] == fingerprint:
                            meeting = cached[1]
                        else:
                            meeting = self._read(
                                fd, directory, identifier, activity.get(identifier), [s[0] for s in signature]
                            )
                            self._row_cache[name] = fingerprint, meeting
                    finally:
                        os.close(fd)
                except Exception:
                    self._row_cache.pop(name, None)
                    self._files.forget(directory)
                    self._preview_cache.pop(name, None)
                    meeting = _unreadable(directory, identifier, activity.get(identifier))
                if meeting is None:
                    self._rows.pop(name, None)
                else:
                    self._rows[name] = meeting
            diagnostics.extend(self._folder_diagnostics.values())
            return LibrarySnapshot(
                tuple(sorted(self._rows.values(), key=_sort)),
                tuple(diagnostics),
                directories=tuple(sorted(self._directories.values())),
            )
        except OSError:
            return LibrarySnapshot(diagnostics=(Diagnostic(self.root, "library-unreadable"),), unavailable=True)
        finally:
            os.close(root_fd)

    def _forget(self, name):
        old = self._rows.pop(name, None) or self._row_cache.get(name, (None, None))[1]
        directory = old.directory if old else self._directories.get(name)
        if directory is not None:
            self._files.forget(directory)
        self._preview_cache.pop(name, None)
        self._row_cache.pop(name, None)
        self._folder_diagnostics.pop(name, None)
        self._directories.pop(name, None)

    def _load(self, fd, directory, name, kind, loader):
        return self._files.get(fd, directory / basename(name), kind, loader)

    def _metadata(self, fd, directory, identifier, origin, diagnostics):
        try:
            raw = self._load(fd, directory, "meeting.json", "json", _json)
        except FileNotFoundError:
            return {}, {}, False
        except (OSError, ValueError):
            diagnostics.append(Diagnostic(directory / "meeting.json", "meeting-metadata-invalid"))
            return {}, {}, False
        if type(raw.get("version")) is int and raw["version"] > 1:
            diagnostics.append(Diagnostic(directory / "meeting.json", "unsupported-version"))
            return raw, {}, True
        speakers = raw.get("speakers", [])
        ids = [s.get("id") for s in speakers if isinstance(s, dict)] if isinstance(speakers, list) else []
        if (
            not valid(raw)
            or not isinstance(raw.get("id"), str)
            or raw["id"].lower() != identifier
            or len(ids) != len(set(ids))
            or (origin == "received") != isinstance(raw.get("received"), dict)
        ):
            diagnostics.append(Diagnostic(directory / "meeting.json", "meeting-metadata-invalid"))
            return {}, {}, False
        return raw, raw, False

    def _capture(self, fd, directory, identifier, names, diagnostics):
        salvaged = False
        try:
            capture = self._load(fd, directory, "session.json", "json", _json)
            if (
                type(capture.get("version")) is not int
                or capture["version"] != 1
                or not isinstance(capture.get("id"), str)
                or capture["id"].lower() != identifier
                or not isinstance(capture.get("tracks"), list)
                or len(capture["tracks"]) > 64
            ):
                raise ValueError("capture-identity-or-version")
            values = capture["tracks"]
        except (OSError, ValueError):
            capture, salvaged = {}, True
            values = [dict(file=name) for name in sorted(names) if name.startswith("track-") and name.endswith(".wav")][
                :64
            ]
            diagnostics.append(Diagnostic(directory / "session.json", "capture-recovered"))
        tracks = []
        identifiers = [
            value.get("id") for value in values if isinstance(value, dict) and isinstance(value.get("id"), str)
        ]
        used = set(identifiers)
        files = set()
        for value in values:
            try:
                if not isinstance(value, dict):
                    raise ValueError("invalid-track")
                name = basename(value.get("file"))
                if not name.startswith("track-") or not name.endswith(".wav"):
                    raise ValueError("invalid-track-name")
                frames, rate, channels = self._load(fd, directory, name, "wave", _wave)
                if any(
                    k in value and value[k] != actual for k, actual in (("sampleRate", rate), ("channels", channels))
                ):
                    raise ValueError("track-format-mismatch")
                offset = value.get("startOffsetMs")
                if type(offset) not in (int, float) or not 0 <= offset <= 86400000 or not math.isfinite(offset):
                    offset = None
                    diagnostics.append(Diagnostic(directory / name, "start-offset-unknown-display-zero"))
                if name in files:
                    raise ValueError("duplicate-track-file")
                files.add(name)
                identity = value.get("id")
                if (
                    not isinstance(identity, str)
                    or not identity
                    or len(identity.encode()) > 200
                    or identifiers.count(identity) != 1
                ):
                    nonce = 0
                    identity = "recovered-" + str(uuid5(NAMESPACE_URL, name))
                    while identity in used:
                        nonce += 1
                        identity = "recovered-" + str(uuid5(NAMESPACE_URL, name + str(nonce)))
                    diagnostics.append(Diagnostic(directory / name, "track-identity-recovered"))
                used.add(identity)
                kind = value.get("kind")
                if not isinstance(kind, str) or kind not in KINDS:
                    kind = {"track-microphone.wav": "microphone", "track-system.wav": "system"}.get(name, "unknown")
                source_identity = value.get("sourceTrackId")
                if source_identity is not None and (
                    not isinstance(source_identity, str) or not source_identity or len(source_identity.encode()) > 200
                ):
                    source_identity = None
                    diagnostics.append(Diagnostic(directory / name, "source-track-identity-invalid"))
                if kind in ("microphone", "local", "system"):
                    source_identity = None
                tracks.append(
                    CaptureTrack(
                        directory / name,
                        frames,
                        rate,
                        offset,
                        value.get("ended"),
                        channels,
                        identity,
                        kind,
                        source_identity,
                    )
                )
            except (OSError, ValueError, TypeError):
                diagnostics.append(Diagnostic(directory, "capture-track-unreadable"))
        return capture, tuple(tracks), salvaged

    def _read(self, fd, directory, identifier, active, names):
        diagnostics = []
        origin = "received" if directory.name.startswith("Received-") else "own"
        raw, metadata, unsupported = self._metadata(fd, directory, identifier, origin, diagnostics)
        if origin == "received" and not metadata:
            return None
        capture, tracks, embedded, audio_file = {}, (), None, None
        embedded_words = {}
        audio_size = None
        duration, started, trusted = None, None, False
        lifecycle = active if origin == "own" and active in ("recording", "saving") else "saved"
        interrupted = None
        if origin == "own":
            capture, tracks, salvaged = self._capture(fd, directory, identifier, names, diagnostics)
            duration = max((t.display_end_ms for t in tracks), default=None)
            started = _date(capture.get("createdAt"))
            gap = capture.get("problemKind") == "gap" or any(
                isinstance(t, dict) and _integer(t.get("gapFrames")) and t["gapFrames"] > 0
                for t in capture.get("tracks", [])
            )
            if (
                salvaged
                or capture.get("state") == "interrupted"
                or capture.get("state") == "recording"
                and lifecycle == "saved"
                or gap
            ):
                interrupted = duration
        else:
            received = metadata["received"]
            started = received["importedAtMs"]
            try:
                name = basename(received["originalName"])
                audio_file = directory / name
                embedded, duration, trusted, embedded_words, audio_size = self._load(fd, directory, name, "opus", _opus)
                created = _date(embedded.created_at)
                if created is not None:
                    started = created
            except (OSError, ValueError):
                diagnostics.append(Diagnostic(directory, "received-audio-unreadable"))
        words, transcript, eta, error = self._transcript(
            fd, directory, metadata, origin, embedded, trusted, embedded_words, diagnostics
        )
        from .speaker_merges import apply_merges

        known = [s["id"] for s in metadata.get("speakers", [])]
        if embedded:
            known.extend(s.id for s in embedded.speakers)
        words = apply_merges(words, metadata.get("speakerMerges", []), known)
        received_name = metadata.get("received", {}).get("originalName") if origin == "received" else None
        title_values = [
            metadata.get("title"),
            capture.get("title"),
            embedded.manifest["meeting"].get("title") if embedded and embedded.manifest else None,
            embedded.tags.get("TITLE") if embedded else None,
            Path(received_name).stem if received_name else None,
        ]
        title = next((t.strip() for t in title_values if isinstance(t, str) and t.strip()), "Untitled meeting")
        reference = (metadata.get("transcript") or {}).get("reference") or {}
        legacy_transcript_file = (
            audio_file
            if (
                origin == "received"
                and bool(words)
                and transcript in ("done", "queued", "running")
                and embedded is not None
                and embedded.is_readable
                and reference.get("kind") == "embedded"
                and reference.get("transcriptId", embedded.default_transcript_id) == embedded.default_transcript_id
            )
            else None
        )
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
            words=tuple(words),
            preview_turns=self._preview(directory.name, words),
            turns=self._preview_cache.get(directory.name, (None, None, ()))[2],
            speakers=_speakers(words, metadata.get("speakers", []), embedded if origin == "received" else None),
            quote_line=_quote(words, metadata.get("quoteOverride"))
            if transcript in ("done", "queued", "running")
            else None,
            tracks=tracks,
            capture_source=capture.get("source") if origin == "own" else None,
            capture_state=capture.get("state") if origin == "own" else None,
            capture_events=read_events(capture.get("events")) if origin == "own" else (),
            audio_file=audio_file,
            audio_size_bytes=audio_size,
            legacy_transcript_file=legacy_transcript_file,
            has_audio=bool(tracks) if origin == "own" else duration is not None,
            metadata_editable=not unsupported,
            diagnostics=tuple(diagnostics),
            metadata=raw,
            notes=tuple(metadata.get("notes", [])),
            extensions=embedded.manifest.get("x", {})
            if embedded and embedded.manifest and isinstance(embedded.manifest.get("x"), dict)
            else {},
        )

    def _preview(self, name, words):
        if not words:
            self._preview_cache.pop(name, None)
            return ()
        cached = self._preview_cache.get(name)
        if cached is not None and cached[0] is words:
            return cached[1]
        # Reader tuples are stat-keyed and immutable to consumers. Group once in
        # the scanner worker; retain only three visible turns for the GTK pane.
        ordered = sorted(words, key=lambda word: word.start_ms)
        # Bound the legacy aside/overlap grouper by speaker fragments, keeping
        # a long monologue intact and enough lookahead for the three previews.
        # Its pairwise work is constant-bounded; full reading keeps every word.
        fragments = 0
        limit = len(ordered)
        for index, word in enumerate(ordered):
            if index == 0 or word.speaker != ordered[index - 1].speaker:
                fragments += 1
                if fragments > 32:
                    limit = index
                    break
        turns = tuple(build_turns(ordered[:limit]))
        result = tuple(PreviewTurn(t.speaker, t.start_ms, t.text) for t in visible_turns(turns)[:3])
        self._preview_cache[name] = words, result, reading_turns(ordered)
        return result

    def _transcript(self, fd, directory, metadata, origin, embedded, trusted, embedded_words, diagnostics):
        value = metadata.get("transcript")
        status = value["status"] if value else None
        reference = (
            value["reference"] if value else dict(kind="sidecar", file="transcript.json", format=portable.WORDS_FORMAT)
        )
        stored = metadata.get("error")
        error = MeetingError(stored["id"], stored["action"]) if stored else None
        words, readable, present = (), False, False
        try:
            if reference is not None and status not in ("none", "failed", "damaged"):
                if (
                    origin == "received"
                    and not trusted
                    and (
                        reference["kind"] != "sidecar"
                        or embedded is None
                        or embedded.state == portable.State.UNKNOWN_FORMAT
                        or embedded.is_readable
                    )
                ):
                    raise ValueError("received-body-untrusted")
                if reference["kind"] == "sidecar":

                    def load_words(stream, info):
                        return _sidecar_words(_json(stream, info, portable.INFLATE_CEILING))

                    words = self._load(fd, directory, "transcript.json", "words", load_words)
                else:
                    if (
                        origin != "received"
                        or reference["file"] != metadata["received"]["originalName"]
                        or embedded is None
                    ):
                        raise ValueError("embedded-reference-mismatch")
                    selected = reference.get("transcriptId", embedded.default_transcript_id)
                    if selected not in embedded_words:
                        try:
                            embedded_words[selected] = tuple(portable.load_words(embedded, selected))
                        except ValueError:
                            embedded_words[selected] = None
                    words = embedded_words[selected]
                    if words is None:
                        raise ValueError("embedded-transcript-unreadable")
                readable, present = True, True
        except FileNotFoundError:
            present = status is not None
        except (OSError, ValueError, KeyError, TypeError):
            present = True
            diagnostics.append(Diagnostic(directory, "transcript-unreadable"))
        if status in ("queued", "running"):
            eta = metadata.get("etaMinutes")
            return words, status, eta if _integer(eta) else None, error
        if status == "failed":
            return (), "failed", None, error or MeetingError("transcribe-failed", "Try again")
        if status == "damaged" or status == "done" and not readable or status is None and present and not readable:
            actionable = error if error and error.action is not None else MeetingError("transcript-damaged", None)
            return (), "damaged", None, actionable
        if status is None:
            status = "done" if readable else "none"
        if origin == "received" and status == "done" and readable and not words and reference.get("kind") == "embedded":
            from .portable_context import empty_word_transcripts

            if empty_word_transcripts(embedded, embedded_words):
                status = "none"
        return (() if status == "none" else words), status, None, error


def _unreadable(directory, identifier, activity):
    if directory.name.startswith("Received-"):
        return None
    return Meeting(
        id=identifier,
        directory=directory,
        title="Untitled meeting",
        started_at_ms=None,
        duration_ms=None,
        lifecycle=activity if activity in ("recording", "saving") else "saved",
        diagnostics=(Diagnostic(directory, "meeting-unreadable"),),
    )


def _sidecar_words(body):
    items = body.get("items")
    if not isinstance(items, list):
        raise ValueError("invalid-transcript-items")
    words = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("speaker"), str)
            or not item["speaker"]
            or not isinstance(item.get("text"), str)
        ):
            raise ValueError("invalid-transcript-word")
        start, end = item.get("startMs"), item.get("endMs")
        if not _integer(start) or not _integer(end) or end < start:
            raise ValueError("invalid-transcript-time")
        words.append(portable.Word(item["speaker"], start, end, item["text"], item.get("lowConfidenceSpeaker") is True))
    return tuple(words)


def _speakers(words, values, embedded):
    from .speaker_merges import speaker_metrics

    metrics = speaker_metrics(words)
    metadata = {s["id"]: s for s in values}
    labels = {s.id: s.label for s in embedded.speakers} if embedded else {}
    first = {}
    for index, word in enumerate(words):
        first[word.speaker] = min(first.get(word.speaker, (word.start_ms, index)), (word.start_ms, index))
    result = []
    for rank, identifier in enumerate(sorted(first, key=first.get)):
        value = metadata.get(identifier, {})
        name = value.get("name")
        if name is None:
            label = labels.get(identifier) if value.get("provenance") != "unnamed" else None
            name = label if label and not re.fullmatch(r"Speaker \d+", label) else f"Speaker {rank + 1}"
        result.append(
            LibrarySpeaker(
                identifier,
                name,
                value.get("provenance"),
                first[identifier][0],
                rank,
                metrics[identifier]["talkMs"],
                metrics[identifier]["sample"],
            )
        )
    return tuple(result)


def _quote(words, override):
    if not words:
        return None
    if isinstance(override, dict):
        text = " ".join(override["text"].split())
        if len(text) >= 2 and (text[0], text[-1]) in (("“", "”"), ('"', '"'), ("‘", "’"), ("'", "'")):
            text = text[1:-1].strip()
        if text:
            return f"“{text}”"
    turns = []
    for index, word in sorted(enumerate(words), key=lambda pair: (pair[1].start_ms, pair[0])):
        if turns and turns[-1][0] == word.speaker:
            turns[-1][2] = word.end_ms
            turns[-1][4].append(word.text.strip())
        else:
            turns.append([word.speaker, word.start_ms, word.end_ms, index, [word.text.strip()]])
    candidates = [t for t in turns if t[1] < 600000]
    if not candidates:
        return None
    chosen = min(candidates, key=lambda t: (-(t[2] - t[1]), t[1], t[3]))
    text = " ".join(" ".join(chosen[4]).split())
    return f"“{text}”" if text else None


def scan_library(root=None, *, capture_activity=None):
    return LibraryScanner(root).scan(capture_activity=capture_activity)


class LibraryCollectionScanner:
    """Read the primary Library plus exactly its durable pending folders."""

    def __init__(self):
        self.scanners = {}

    @property
    def root(self):
        return library_root()

    @property
    def roots(self):
        from .library_location import roots

        return roots()

    def scan(self, *, capture_activity=None, changed_directories=None):
        import hashlib
        from dataclasses import replace

        from .library_location import load, roots

        value = load()
        current_roots = roots(value)
        pending = {Path(e["source"]).resolve() for e in value["pending"]}
        meetings, diagnostics, directories, seen = [], [], [], set()
        unavailable = False
        for root in current_roots:
            scanner = self.scanners.setdefault(root, LibraryScanner(root))
            changed = (
                None
                if changed_directories is None
                else {p for p in changed_directories if Path(p).parent == root.resolve()}
            )
            snapshot = scanner.scan(capture_activity=capture_activity, changed_directories=changed)
            unavailable |= snapshot.unavailable and root == current_roots[0]
            diagnostics.extend(snapshot.diagnostics)
            for meeting in snapshot.meetings:
                if root != current_roots[0] and meeting.directory.resolve() not in pending:
                    continue
                if meeting.key in seen:
                    meeting = replace(
                        meeting, collection_key=meeting.key + "@" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
                    )
                seen.add(meeting.key)
                meetings.append(meeting)
            directories.extend(snapshot.directories)
        for root in self.scanners.keys() - set(current_roots):
            del self.scanners[root]
        return LibrarySnapshot(tuple(sorted(meetings, key=_sort)), tuple(diagnostics), unavailable, tuple(directories))
