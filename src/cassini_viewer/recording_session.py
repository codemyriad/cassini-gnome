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


def label_text(value):
    return "".join(c for c in value if not unicodedata.category(c).startswith("C"))[:200]


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
        self._last_checkpoint = 0
        self.checkpoint()

    def checkpoint(self):
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
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._last_checkpoint = self.clock()

    def add_track(self, identifier, label, kind, rate, channels):
        if (
            self.finished
            or not isinstance(identifier, str)
            or not identifier
            or len(identifier.encode()) > 200
            or identifier in self.writers
            or not isinstance(kind, str)
            or kind not in KINDS
            or type(rate) is not int
            or not 8000 <= rate <= 192000
            or type(channels) is not int
            or channels not in (1, 2)
        ):
            raise ProtocolError("source", "Invalid or duplicate audio source")
        label = label_text(optional_string(label, kind, "source"))
        if len(self.writers) >= 64:
            raise ProtocolError("limit", "A session supports at most 64 sources")
        filename = f"track-{uuid4()}.wav"
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
                "labelSource": "browser" if self.manifest["source"] == "chromium" else "device",
            }
        )
        self.checkpoint()

    def append(self, identifier, data, offset):
        if (
            self.finished
            or not isinstance(identifier, str)
            or identifier not in self.writers
            or type(offset) not in (int, float)
            or not 0 <= offset <= 86400000
            or not math.isfinite(offset)
            or not data
            or len(data) > 262144
        ):
            raise ProtocolError("chunk", "Invalid audio chunk or timestamp")
        writer = self.writers[identifier]
        if len(data) % (writer.channels * 2):
            raise ProtocolError("chunk", "Incomplete PCM frame")
        track = next(t for t in self.manifest["tracks"] if t["id"] == identifier)
        if track.get("startOffsetMs") is None:
            track["startOffsetMs"] = offset
        frame = (offset - track["startOffsetMs"]) * writer.rate / 1000
        target = math.floor(frame + 0.5) if frame >= 0 else math.ceil(frame - 0.5)
        delta = target - writer.frames
        tolerance = writer.rate // 50
        if delta < -tolerance:
            raise ProtocolError("chunk", "Audio chunks arrived out of order")
        if delta > tolerance:
            if delta > writer.rate * 60:
                raise ProtocolError("chunk", "Audio source was interrupted for more than a minute")
            writer.silence(delta)
        writer.append(data)
        track["frames"] = writer.frames
        if self.clock() - self._last_checkpoint >= 1:
            self.checkpoint()

    def finish(self, problem=None, *, problem_kind=None):
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
        self.checkpoint()
        if failure:
            raise failure


class BrowserHost:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path.home() / "Music" / "Cassini"
        self.session = None
        self.sequences = {}
        self.stopped = False

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
            self.session.add_track(
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
            self.session.append(identifier, data, message.get("offsetMs"))
            self.sequences[identifier] += 1
        elif kind == "stop":
            self.session.finish(optional_string(message.get("problem"), None, "state"))
            self.stopped = True
            result = {"type": "stopped", "path": str(self.session.directory)}
            if "problem" in self.session.manifest:
                result["problem"] = self.session.manifest["problem"]
            return result
        return {"type": "ack"}

    def close(self, problem="Browser disconnected before recording was stopped.", *, problem_kind=None):
        if self.session and not self.stopped:
            self.session.finish(problem, problem_kind=problem_kind)
            self.stopped = True


def wave_info(path, directory):
    """Read the v1 PCM header; complete frames on disk win over stale sizes."""
    if path.resolve().parent != directory.resolve():
        raise ValueError("Recording audio is outside the session directory")
    with path.open("rb") as audio:
        header = audio.read(44)
        size = os.fstat(audio.fileno()).st_size
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


def recover_session(directory):
    files = sorted(directory.glob("track-*.wav"))
    if len(files) > 64:
        raise ValueError("Too many recording tracks to recover")
    tracks = []
    for file in files:
        try:
            info = wave_info(file, directory)
        except (OSError, ValueError):
            continue
        tracks.append(
            dict(id=file.stem, file=file.name, label="Recovered audio", kind="media", labelSource="recovered", **info)
        )
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


def load_session(path):
    """Load or salvage a session read-only; never infer missing labels or offsets."""
    path = Path(path).absolute()
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Recording manifest is too large")
        manifest = strict_json(path.read_bytes())
        if not isinstance(manifest, dict) or not isinstance(manifest.get("tracks"), list):
            raise ValueError("Invalid recording metadata")
    except (OSError, ValueError):
        return recover_session(path.parent)
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("Unsupported recording session")
    tracks = manifest["tracks"]
    if len(tracks) > 64:
        raise ValueError("Invalid recording tracks")
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("file"), str):
            raise ValueError("Invalid recording track")
        filename = track["file"]
        if Path(filename).name != filename or not filename.endswith(".wav"):
            raise ValueError("Recording audio is outside the session directory")
        info = wave_info(path.parent / filename, path.parent)
        if track.get("sampleRate") != info["sampleRate"] or track.get("channels") != info["channels"]:
            raise ValueError("Audio format differs from recording metadata")
        track.update(info)
        offset = track.get("startOffsetMs")
        if offset is not None and (
            type(offset) not in (int, float) or not 0 <= offset <= 86400000 or not math.isfinite(offset)
        ):
            raise ValueError("Invalid recording track offset")
    return manifest
