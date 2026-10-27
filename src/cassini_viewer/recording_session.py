# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable recording sessions, shared with Cassini for Mac (protocol version 1).

Track IDs are opaque source identifiers, never participant identities or paths.
WAV headers are checkpointed after every write so interrupted audio is playable.
"""

import base64
import json
import math
import os
import struct
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

MAX_MESSAGE = 512 * 1024
KINDS = {"microphone", "system", "application", "local", "remote", "media", "tab"}


def read_message(source):
    def exact(size, eof=False):
        result = bytearray()
        while len(result) < size:
            part = source.read(size - len(result))
            if not part:
                if eof and not result:
                    return None
                raise ValueError("Truncated native message")
            result.extend(part)
        return result

    header = exact(4, eof=True)
    if header is None:
        return None
    (size,) = struct.unpack("=I", header)
    if not 0 < size <= MAX_MESSAGE:
        raise ValueError("Invalid native message size")
    message = json.loads(exact(size))
    if not isinstance(message, dict):
        raise ValueError("Expected a message object")
    return message


def write_message(destination, message):
    data = json.dumps(message, allow_nan=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ValueError("Native response is too large")
    destination.write(struct.pack("=I", len(data)) + data)
    destination.flush()


class WAVWriter:
    def __init__(self, path, rate, channels):
        self.rate, self.channels, self.size = rate, channels, 0
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
        if self.size + len(data) > 0xFFFFFFFF - 36:
            raise ValueError("Track reached the WAV size limit. Start a new recording.")
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

    def close(self):
        if not self.file.closed:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()


class RecordingSession:
    def __init__(self, parent, title="Browser recording", source="chromium"):
        identifier = str(uuid4())
        self.directory = Path(parent) / f"Recording-{identifier}"
        self.directory.mkdir(mode=0o700, parents=True)
        self.manifest = {
            "version": 1,
            "id": identifier,
            "createdAt": datetime.now(UTC).isoformat(),
            "title": str(title)[:500],
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
        temporary.write_text(json.dumps(self.manifest, indent=2, allow_nan=False) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
        self._last_checkpoint = time.monotonic()

    def add_track(self, identifier, label, kind, rate, channels):
        if (
            self.finished
            or not isinstance(identifier, str)
            or not identifier
            or len(identifier.encode()) > 200
            or identifier in self.writers
            or len(self.writers) >= 64
            or kind not in KINDS
            or type(rate) is not int
            or not 8000 <= rate <= 192000
            or type(channels) is not int
            or channels not in (1, 2)
        ):
            raise ValueError("Invalid or duplicate audio source")
        filename = f"track-{uuid4()}.wav"
        self.writers[identifier] = WAVWriter(self.directory / filename, rate, channels)
        self.manifest["tracks"].append(
            {
                "id": identifier,
                "label": str(label)[:500],
                "kind": kind,
                "file": filename,
                "sampleRate": rate,
                "channels": channels,
                "startOffsetMs": None,
                "frames": 0,
            }
        )
        self.checkpoint()

    def append(self, identifier, data, offset):
        if (
            self.finished
            or identifier not in self.writers
            or type(offset) not in (int, float)
            or not math.isfinite(offset)
            or not 0 <= offset <= 86400000
            or not data
            or len(data) > 262144
        ):
            raise ValueError("Invalid audio chunk or timestamp")
        writer = self.writers[identifier]
        if len(data) % (writer.channels * 2):
            raise ValueError("Incomplete PCM frame")
        track = next(t for t in self.manifest["tracks"] if t["id"] == identifier)
        if track["startOffsetMs"] is None:
            track["startOffsetMs"] = offset
        target = round((offset - track["startOffsetMs"]) * writer.rate / 1000)
        delta = target - writer.frames
        tolerance = writer.rate // 50
        if delta < -tolerance:
            raise ValueError("Audio chunks arrived out of order")
        if delta > tolerance:
            if delta > writer.rate * 60:
                raise ValueError("Audio source was interrupted for more than a minute")
            writer.silence(delta)
        writer.append(data)
        track["frames"] = writer.frames
        if time.monotonic() - self._last_checkpoint >= 1:
            self.checkpoint()

    def finish(self, problem=None):
        if self.finished:
            return
        self.finished = True
        failure = None
        for writer in self.writers.values():
            try:
                writer.close()
            except OSError as error:
                failure = failure or str(error)
        problem = problem or failure
        if not any(t["frames"] for t in self.manifest["tracks"]):
            problem = problem or "No audio samples were received. Check the selected sources and permissions."
        if problem:
            self.manifest["problem"] = str(problem)[:1000]
        self.manifest["state"] = "interrupted" if problem else "completed"
        self.checkpoint()


class BrowserHost:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path.home() / "Music" / "Cassini"
        self.session = None
        self.sequences = {}
        self.stopped = False

    def handle(self, message):
        kind = message.get("type")
        if kind == "start":
            if type(message.get("version", 1)) is not int or message.get("version", 1) != 1:
                raise ValueError("Unsupported browser recording protocol version")
            if self.session:
                raise ValueError("A recording has already started")
            self.session = RecordingSession(self.directory, message.get("title", "Browser recording"))
            return {"type": "started", "path": str(self.session.directory)}
        if self.session is None or self.stopped:
            raise ValueError("No active recording")
        if kind == "track":
            identifier = message.get("id")
            self.session.add_track(
                identifier,
                message.get("label", message.get("kind")),
                message.get("kind"),
                message.get("sampleRate"),
                message.get("channels"),
            )
            self.sequences[identifier] = 0
        elif kind == "audio":
            identifier = message.get("id")
            sequence = message.get("sequence")
            if type(sequence) is not int or identifier not in self.sequences or self.sequences[identifier] != sequence:
                raise ValueError("Missing or out-of-order audio chunk")
            encoded = message.get("pcm")
            if not isinstance(encoded, str) or len(encoded) > 349528:
                raise ValueError("Invalid PCM payload")
            self.session.append(identifier, base64.b64decode(encoded, validate=True), message.get("offsetMs"))
            self.sequences[identifier] += 1
        elif kind == "stop":
            self.session.finish(message.get("problem"))
            self.stopped = True
            result = {"type": "stopped", "path": str(self.session.directory)}
            if "problem" in self.session.manifest:
                result["problem"] = self.session.manifest["problem"]
            return result
        else:
            raise ValueError("Unknown browser message")
        return {"type": "ack"}

    def close(self, problem="Browser disconnected before recording was stopped."):
        if self.session and not self.stopped:
            self.session.finish(problem)
            self.stopped = True


def load_session(path):
    """Validate an on-disk session before using its track paths or durations."""
    path = Path(path).resolve()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Recording manifest is too large")
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Unsupported recording session")
    tracks = manifest.get("tracks")
    if not isinstance(tracks, list) or len(tracks) > 64:
        raise ValueError("Invalid recording tracks")
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("file"), str):
            raise ValueError("Invalid recording track")
        target = path.parent / track["file"]
        if target.resolve().parent != path.parent or not target.is_file():
            raise ValueError("Recording track is missing or outside the session directory")
        import wave

        with wave.open(str(target)) as audio:
            if (
                audio.getsampwidth() != 2
                or audio.getnchannels() not in (1, 2)
                or not 8000 <= audio.getframerate() <= 192000
            ):
                raise ValueError("Unsupported recording audio")
            # The WAV header is authoritative after an interrupted checkpoint.
            track["frames"] = audio.getnframes()
            track["sampleRate"] = audio.getframerate()
            track["channels"] = audio.getnchannels()
        offset = track.get("startOffsetMs") or 0
        if type(offset) not in (int, float) or not math.isfinite(offset) or not 0 <= offset <= 86400000:
            raise ValueError("Invalid recording track offset")
    return manifest
