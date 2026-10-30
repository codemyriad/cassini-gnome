# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable recording sessions, shared with Cassini for Mac (protocol version 1).

Track IDs are opaque source identifiers, never participant identities or paths.
WAV headers are checkpointed after every write so interrupted audio is playable.
"""

import base64
import binascii
import json
import math
import os
import struct
import time
import unicodedata
from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

MAX_MESSAGE = 512 * 1024
KINDS = {"microphone", "system", "application", "local", "remote", "media", "tab"}


class ProtocolError(ValueError):
    """A stable wire error code with a human-readable explanation."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def optional_string(value, default, code):
    if value is None:
        return default
    if not isinstance(value, str):
        raise ProtocolError(code, "Expected a string or null")
    return value


def stop_reason(message):
    from .session_events import END_REASONS

    reason = message.get("reason", "user")
    if not isinstance(reason, str) or reason not in END_REASONS:
        raise ProtocolError("state", "Invalid recording end reason")
    return reason


def label_text(value):
    return "".join(c for c in value if unicodedata.category(c) not in ("Cc", "Cf"))[:200]


def source_parameters(identifier, label, kind, rate, channels, identifiers):
    if (
        not isinstance(identifier, str)
        or not identifier
        or len(identifier.encode()) > 200
        or identifier in identifiers
        or not isinstance(kind, str)
        or kind not in KINDS
        or type(rate) is not int
        or not 8000 <= rate <= 192000
        or type(channels) is not int
        or channels not in (1, 2)
    ):
        raise ProtocolError("source", "Invalid or duplicate audio source")
    if len(identifiers) >= 64:
        raise ProtocolError("limit", "A session supports at most 64 sources")
    return label_text(optional_string(label, kind, "source"))


def chunk_plan(rate, channels, frames, start, data, offset):
    """Validate wire geometry even when the selected side is not being saved."""
    if (
        type(offset) not in (int, float)
        or not 0 <= offset <= 86400000
        or not math.isfinite(offset)
        or not data
        or len(data) > 262144
    ):
        raise ProtocolError("chunk", "Invalid audio chunk or timestamp")
    if len(data) % (channels * 2):
        raise ProtocolError("chunk", "Incomplete PCM frame")
    start = offset if start is None else start
    frame = (offset - start) * rate / 1000
    target = math.floor(frame + 0.5) if frame >= 0 else math.ceil(frame - 0.5)
    delta, tolerance = target - frames, rate // 50
    if delta < -tolerance:
        raise ProtocolError("chunk", "Audio chunks arrived out of order")
    if delta > rate * 60:
        raise ProtocolError("chunk", "Audio source was interrupted for more than a minute")
    return start, delta if delta > tolerance else 0


def strict_json(data):
    def reject_constant(value):
        raise ProtocolError("framing", f"Invalid JSON constant: {value}")

    def unique_members(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("framing", "Duplicate JSON member")
            result[key] = value
        return result

    try:
        result = json.loads(data.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_members)
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if isinstance(value, (dict, list)):
                if depth >= 64:
                    raise ProtocolError("framing", "JSON nesting exceeds 64 levels")
                children = list(value.keys()) + list(value.values()) if isinstance(value, dict) else value
                pending.extend((child, depth + 1) for child in children)
            elif isinstance(value, str):
                value.encode("utf-8")  # Reject unpaired surrogate escapes too.
        return result
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError("framing", "Invalid UTF-8 JSON") from error


def read_message(source):
    def exact(size, eof=False):
        result = bytearray()
        while len(result) < size:
            part = source.read(size - len(result))
            if not part:
                if eof and not result:
                    return None
                raise ProtocolError("framing", "Truncated native message")
            result.extend(part)
        return result

    header = exact(4, eof=True)
    if header is None:
        return None
    (size,) = struct.unpack("=I", header)
    if not 0 < size <= MAX_MESSAGE:
        raise ProtocolError("framing", "Invalid native message size")
    message = strict_json(exact(size))
    if not isinstance(message, dict):
        raise ProtocolError("framing", "Expected a message object")
    return message


def write_message(destination, message):
    data = json.dumps(message, allow_nan=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ProtocolError("framing", "Native response is too large")
    destination.write(struct.pack("=I", len(data)) + data)
    destination.flush()


class WAVWriter:
    def __init__(self, path, rate, channels, max_bytes=0xFFFFFFFF - 36):
        self.rate, self.channels, self.size = rate, channels, 0
        self.max_bytes = max_bytes
        self.file = open(path, "x+b")  # noqa: SIM115 — owned until session finalization
        os.chmod(path, 0o600)
        self._header()

    @property
    def frames(self):
        return self.size // (self.channels * 2)

    def _header(self):
        self.file.seek(0)
        self.file.write(
            struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF",
                self.size + 36,
                b"WAVE",
                b"fmt ",
                16,
                1,
                self.channels,
                self.rate,
                self.rate * self.channels * 2,
                self.channels * 2,
                16,
                b"data",
                self.size,
            )
        )
        self.file.flush()

    def append(self, data):
        if self.size + len(data) > self.max_bytes:
            raise ProtocolError("limit", "Track reached the WAV size limit. Start a new recording.")
        self.file.seek(44 + self.size)
        self.file.write(data)
        self.size += len(data)
        self._header()

    def silence(self, frames):
        remaining = frames * self.channels * 2
        while remaining:
            count = min(remaining, 16384)
            self.append(bytes(count))
            remaining -= count

    def sync(self):
        if not self.file.closed:
            self.file.flush()
            os.fsync(self.file.fileno())

    def close(self):
        try:
            self.sync()
        finally:
            self.file.close()


class RecordingSession:
    def __init__(
        self, parent, title="Browser recording", source="chromium", *, clock=time.monotonic, writer_factory=WAVWriter
    ):
        from contextlib import nullcontext

        from .library_location import capture_lease, primary, routing

        self._lease_fd = None
        default_parent = parent is None
        with routing() if parent is None else nullcontext():
            parent = primary().resolve() if parent is None else parent
            self.clock = clock
            self.writer_factory = writer_factory
            identifier = str(uuid4())
            self.directory = Path(parent) / f"Recording-{identifier}"
            self.directory.mkdir(mode=0o700, parents=True)
            self.manifest = {
                "version": 1,
                "id": identifier,
                "createdAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "title": optional_string(title, "Browser recording", "state")[:500],
                "source": source,
                "state": "recording",
                "tracks": [],
            }
            self.writers = {}
            self.finished = False
            self._native_io_error = None
            self._changing = False
            self._last_checkpoint = 0
            self._checkpoint_generation = 0
            self.checkpoint()
            self._started_at = self._last_checkpoint
            if default_parent:
                self._lease_fd = capture_lease(identifier)

    def checkpoint(self):
        if self._changing:
            return
        path = self.directory / "session.json"
        temporary = path.with_suffix(".tmp")
        for writer in self.writers.values():
            writer.sync()
        with temporary.open("w", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            output.write(json.dumps(self.manifest, indent=2, allow_nan=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        self._checkpoint_generation += 1
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._last_checkpoint = self.clock()

    @contextmanager
    def change(self, events):
        """Publish S-6 events and their track changes in one synced manifest.

        Callers hold their capture lock. A crash before rename leaves the old
        manifest; a crash after rename leaves both the new tracks and events.
        A failed durability barrier never removes files a renamed manifest may
        already reference, and is reported to the capture owner before use/ACK.
        """
        from .session_events import append_events

        if self.finished or self._changing:
            raise ProtocolError("state", "No source change is available")
        previous, identifiers = deepcopy(self.manifest), set(self.writers)
        append_events(self.manifest, events)
        generation = self._checkpoint_generation
        self._changing = True
        try:
            yield
            self._changing = False
            self.checkpoint()
        except BaseException:
            added = [t for t in self.manifest["tracks"] if t["id"] not in identifiers]
            self.manifest = previous
            self._changing = False
            published = self._checkpoint_generation != generation
            if published:
                # Rename may already have exposed the proposed tracks. Restore
                # the old working selection if storage permits, but retain all
                # proposed files even if this recovery checkpoint also fails.
                with suppress(OSError):  # Keep the original durability error.
                    self.checkpoint()
            for track in added:
                writer = self.writers.pop(track["id"])
                with suppress(OSError):
                    writer.close()
                if not published:
                    (self.directory / track["file"]).unlink()
            raise
        finally:
            self._changing = False

    def end_track(self, identifier):
        track = next((t for t in self.manifest["tracks"] if t["id"] == identifier), None)
        if self.finished or track is None:
            raise ProtocolError("source", "No active audio source")
        track["ended"] = True
        if track.get("startOffsetMs") is not None:
            track["endOffsetMs"] = track["startOffsetMs"] + track["frames"] * 1000 / track["sampleRate"]
        self.checkpoint()

    def add_track(self, identifier, label, kind, rate, channels, *, filename=None, label_source=None):
        if self.finished:
            raise ProtocolError("source", "Invalid or duplicate audio source")
        label = source_parameters(identifier, label, kind, rate, channels, self.writers)
        if label_source is not None and label_source not in ("browser", "device"):
            raise ProtocolError("source", "Invalid audio source label provenance")
        if filename is not None and filename not in ("track-microphone.wav", "track-system.wav"):
            raise ProtocolError("source", "Invalid native track filename")
        filename = filename or f"track-{uuid4()}.wav"
        self.writers[identifier] = self.writer_factory(self.directory / filename, rate, channels)
        self.manifest["tracks"].append(
            {
                "id": identifier,
                "label": label,
                "kind": kind,
                "file": filename,
                "sampleRate": rate,
                "channels": channels,
                "frames": 0,
                "labelSource": label_source or ("browser" if self.manifest["source"] == "chromium" else "device"),
            }
        )
        self.checkpoint()

    def append(self, identifier, data, offset):
        if self.finished or not isinstance(identifier, str) or identifier not in self.writers:
            raise ProtocolError("chunk", "Invalid audio chunk or timestamp")
        writer = self.writers[identifier]
        track = next(t for t in self.manifest["tracks"] if t["id"] == identifier)
        if track.get("ended"):
            raise ProtocolError("chunk", "Audio source has ended")
        start, delta = chunk_plan(writer.rate, writer.channels, writer.frames, track.get("startOffsetMs"), data, offset)
        track["startOffsetMs"] = start
        if delta:
            writer.silence(delta)
        writer.append(data)
        track["frames"] = writer.frames
        if self.clock() - self._last_checkpoint >= 1:
            self.checkpoint()

    def append_native(self, identifier, data, offset, *, gap_frames=0):
        if self.finished or identifier not in self.writers:
            raise ProtocolError("chunk", "Invalid native audio source")
        writer = self.writers[identifier]
        track = next(t for t in self.manifest["tracks"] if t["id"] == identifier)
        if type(gap_frames) is not int or gap_frames < 0 or 0 < gap_frames < writer.rate or track.get("ended"):
            raise ProtocolError("chunk", "Invalid native input gap")
        # Validate before changing the WAV. Explicit native gaps can be longer
        # than the browser wire's bounded timestamp padding allowance.
        chunk_plan(writer.rate, writer.channels, writer.frames + gap_frames, track.get("startOffsetMs"), data, offset)
        if gap_frames and track.get("startOffsetMs") is None:
            raise ProtocolError("chunk", "A native gap needs a first-buffer anchor")
        try:
            # An S-6 span can declare its selection boundary before the first
            # input arrives. Preserve that known prefix exactly, including
            # sub-20 ms latency; it precedes the first-buffer gap anchor.
            if not writer.frames and not gap_frames and track.get("startOffsetMs") is not None:
                prefix = max(0, round((offset - track["startOffsetMs"]) * writer.rate / 1000))
                if prefix:
                    writer.silence(prefix)
                    track["frames"] = writer.frames
            if gap_frames:
                before = writer.frames
                try:
                    writer.silence(gap_frames)
                finally:
                    written = writer.frames - before
                    if written:
                        track["gapFrames"] = track.get("gapFrames", 0) + written
                    track["frames"] = writer.frames
            self.append(identifier, data, offset)
        except Exception as error:
            self._native_io_error = str(error)
            track["frames"] = writer.frames
            raise

    def finish(self, problem=None, *, problem_kind=None, reason=None, at_ms=None):
        from .capture_sources import change_time
        from .session_events import END_REASONS, append_events

        if reason is not None and (not isinstance(reason, str) or reason not in END_REASONS):
            raise ProtocolError("state", "Invalid recording end reason")
        if at_ms is not None and (type(at_ms) not in (int, float) or not math.isfinite(at_ms) or at_ms < 0):
            raise ProtocolError("state", "Invalid recording end time")
        problem = optional_string(problem, None, "state") or None
        if self.finished:
            return
        self.finished = True
        failure = None
        for writer in self.writers.values():
            try:
                writer.close()
            except OSError as error:
                failure = failure or error
        for track in self.manifest["tracks"]:
            track["ended"] = True
            if track.get("startOffsetMs") is not None:
                track["endOffsetMs"] = track["startOffsetMs"] + track["frames"] * 1000 / track["sampleRate"]
        gaps = [t for t in self.manifest["tracks"] if t.get("gapFrames", 0)]
        if self._native_io_error:
            problem, problem_kind = problem or self._native_io_error, "io"
        elif not problem and gaps:
            problem = " ".join(
                f"{t['gapFrames'] * 1000 // t['sampleRate']} ms of {t['kind']} input time was preserved as silence."
                for t in gaps
            )
            problem_kind = "gap"
        problem_kind = problem_kind or ("capture" if problem else None)
        if failure:
            problem, problem_kind = str(failure), "io"
        if not any(t["frames"] for t in self.manifest["tracks"]) and not problem:
            problem, problem_kind = (
                "No audio samples were received. Check the selected sources and permissions.",
                "empty",
            )
        if problem:
            self.manifest.update(problem=problem[:1000], problemKind=problem_kind)
        self.manifest["state"] = "interrupted" if problem else "completed"
        # A warning about earlier missing PCM does not change the explicit stop
        # cause. A failed capture or disk write is an actual error termination.
        end_reason = reason or ("error" if problem and problem_kind != "gap" else "user")
        if failure or self._native_io_error:
            end_reason = "error"
        elapsed = max(0, (self.clock() - self._started_at) * 1000) if at_ms is None else at_ms
        append_events(self.manifest, [dict(type="end", atMs=change_time(self, elapsed), reason=end_reason)])
        try:
            self.checkpoint()
        finally:
            if self._lease_fd is not None:
                os.close(self._lease_fd)
                self._lease_fd = None
        if failure:
            raise failure


class BrowserHost:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else None
        self.session = None
        self.sequences = {}
        self.stopped = False
        self.audio_sink = None

    def handle(self, message):
        kind = message.get("type")
        if kind not in ("start", "track", "audio", "stop"):
            raise ProtocolError("version", "Unknown browser message type")
        if kind == "start":
            if self.session:
                raise ProtocolError("state", "A recording has already started")
            version = message.get("version")
            version = 1 if version is None else version
            if type(version) is not int or version != 1:
                raise ProtocolError("version", "Unsupported browser recording protocol version")
            title = optional_string(message.get("title"), "Browser recording", "state")
            self.session = RecordingSession(self.directory, title)
            return {"type": "started", "path": str(self.session.directory)}
        if self.session is None or self.stopped:
            raise ProtocolError("state", "No active recording")
        if kind == "track":
            identifier = message.get("id")
            (self.audio_sink or self.session).add_track(
                identifier,
                message.get("label"),
                message.get("kind"),
                message.get("sampleRate"),
                message.get("channels"),
            )
            self.sequences[identifier] = 0
        elif kind == "audio":
            identifier = message.get("id")
            if not isinstance(identifier, str) or identifier not in self.sequences:
                raise ProtocolError("source", "Unknown audio source")
            sequence = message.get("sequence")
            if (
                type(sequence) is not int
                or not 0 <= sequence <= 9007199254740991
                or self.sequences[identifier] != sequence
            ):
                raise ProtocolError("sequence", "Missing or out-of-order audio chunk")
            encoded = message.get("pcm")
            if not isinstance(encoded, str) or len(encoded) > 349528:
                raise ProtocolError("chunk", "Invalid PCM payload")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ProtocolError("chunk", "Invalid base64 PCM") from error
            (self.audio_sink or self.session).append(identifier, data, message.get("offsetMs"))
            self.sequences[identifier] += 1
        elif kind == "stop":
            self.session.finish(optional_string(message.get("problem"), None, "state"), reason=stop_reason(message))
            self.stopped = True
            result = {"type": "stopped", "path": str(self.session.directory)}
            if "problem" in self.session.manifest:
                result["problem"] = self.session.manifest["problem"]
            return result
        return {"type": "ack"}

    def close(self, problem="Browser disconnected before recording was stopped.", *, problem_kind=None):
        if self.session and not self.stopped:
            self.session.finish(problem, problem_kind=problem_kind, reason="error")
            self.stopped = True


def _wave_at(directory_fd, name):
    """The v1 PCM header of a basename in an opened session folder, never following a link.

    Complete frames on disk win over stale sizes.
    """
    import errno

    from .library_files import regular

    try:
        with regular(directory_fd, name) as (stream, info):
            return _wave_fields(stream, info.st_size)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("Recording audio is outside the session directory") from None
        raise


def _wave_fields(audio, size):
    header = audio.read(44)
    if len(header) != 44:
        raise ValueError("Incomplete recording WAV header")
    riff, _, wave, fmt, fmt_size, encoding, channels, rate, byte_rate, alignment, bits, data, _ = struct.unpack(
        "<4sI4s4sIHHIIHH4sI", header
    )
    if (
        riff != b"RIFF"
        or wave != b"WAVE"
        or fmt != b"fmt "
        or data != b"data"
        or fmt_size != 16
        or encoding != 1
        or bits != 16
        or channels not in (1, 2)
        or not 8000 <= rate <= 192000
        or alignment != channels * 2
        or byte_rate != rate * channels * 2
    ):
        raise ValueError("Unsupported recording PCM WAV format")
    return {"sampleRate": rate, "channels": channels, "frames": (size - 44) // alignment}


def recover_session(directory, pairs=None, *, directory_fd=None):
    """Salvage a session folder, opened without following a link before any read."""
    from . import track_source

    fd = os.open(directory, track_source.NOFOLLOW_DIRECTORY) if directory_fd is None else directory_fd
    try:
        names = sorted(n for n in os.listdir(fd) if n.startswith("track-") and n.endswith(".wav"))
        if len(names) > 64:
            raise ValueError("Too many recording tracks to recover")
        tracks = []
        for name in names:
            try:
                info = _wave_at(fd, name)
            except (OSError, ValueError):
                continue
            tracks.append(
                dict(
                    id=Path(name).stem,
                    file=name,
                    label="Recovered audio",
                    kind="media",
                    labelSource="recovered",
                    **info,
                )
            )
        tracks += recover_compacted(fd, names, pairs)
    finally:
        if directory_fd is None:
            os.close(fd)
    if not tracks:
        raise ValueError("No readable recording metadata or PCM WAV tracks were found")
    return dict(
        version=1,
        id=str(uuid4()),
        createdAt=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        title="Recovered recording",
        source="recovered",
        state="interrupted",
        problemKind="recovery",
        problem="Recovered audio from damaged or missing metadata. Source labels and relative start times are unavailable.",
        tracks=tracks,
    )


def recover_compacted(directory_fd, wavs, pairs=None):
    """S-12 §5 salvage of committed `tracks/*.opus` spans without a usable session.json.

    Surviving `sourceFile` records (given, or read from a damaged session.json
    that still parses) pair Opus spans with their WAVs, as the scanner does.
    Without a record an Opus span is used only when no WAV is left (S-12 §11).
    Nothing is ever deleted here.
    """
    from . import track_source

    names = track_source.recovered_opus(directory_fd, limit=65)
    if pairs is None:
        pairs = track_source.surviving_pairs(directory_fd)
    names = [v["file"] for v in track_source.salvage_values(wavs, names, pairs, limit=None) if v.get("codec") == "opus"]
    if len(names) > 64:
        raise ValueError("Too many recording tracks to recover")
    tracks = []
    for name in names:
        value = dict(file=name, codec="opus", decodedRate=track_source.OPUS_RATE)
        try:
            info = track_source.inspect_at(directory_fd, value, recovered=True)
        except (OSError, ValueError):
            continue
        tracks.append(
            dict(
                value,
                id=Path(name).stem,
                label="Recovered audio",
                kind="media",
                labelSource="recovered",
                sampleRate=info.sample_rate,
                channels=info.channels,
                frames=info.frames,
            )
        )
    return tracks


def load_session(path):
    """Load or salvage a session read-only; never infer missing labels or offsets.

    The session folder is opened without following a link before anything in
    it is read, and every file is read relative to that descriptor.
    """
    from . import track_source
    from .library_files import regular

    path = Path(path).absolute()
    fd = os.open(path.parent, track_source.NOFOLLOW_DIRECTORY)
    try:
        return _load_session(path, fd, regular, track_source)
    finally:
        os.close(fd)


def _load_session(path, fd, regular, track_source):
    try:
        with regular(fd, path.name) as (stream, info):
            if info.st_size > 1024 * 1024:
                raise ValueError("Recording manifest is too large")
            manifest = strict_json(stream.read())
        if not isinstance(manifest, dict) or not isinstance(manifest.get("tracks"), list):
            raise ValueError("Invalid recording metadata")
    except (OSError, ValueError):
        return recover_session(path.parent, directory_fd=fd)
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("Unsupported recording session")
    tracks = manifest["tracks"]
    if len(tracks) > 64:
        raise ValueError("Invalid recording tracks")
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("file"), str):
            raise ValueError("Invalid recording track")
        if "codec" in track:
            # S-12 §5 compacted span: source sampleRate/frames stay as written;
            # the confined Opus file must match them in 48 kHz time.
            try:
                track_source.inspect_at(fd, track)
            except ValueError as error:
                raise ValueError(f"Invalid compacted recording track: {error}") from None
        else:
            filename = track["file"]
            if Path(filename).name != filename or not filename.endswith(".wav"):
                raise ValueError("Recording audio is outside the session directory")
            info = _wave_at(fd, filename)
            if track.get("sampleRate") != info["sampleRate"] or track.get("channels") != info["channels"]:
                raise ValueError("Audio format differs from recording metadata")
            track.update(info)
        offset = track.get("startOffsetMs")
        if offset is not None and (
            type(offset) not in (int, float) or not 0 <= offset <= 86400000 or not math.isfinite(offset)
        ):
            raise ValueError("Invalid recording track offset")
    return manifest
