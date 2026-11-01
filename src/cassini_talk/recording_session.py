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

from .i18n import gettext as _

MAX_MESSAGE = 512 * 1024
KINDS = {"microphone", "system", "application", "local", "remote", "media", "tab"}


class ProtocolError(ValueError):
    """A stable wire error code with a human-readable explanation."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SourceIOError(OSError):
    """A known track writer failed; shared session checkpoint errors stay fatal."""

    def __init__(self, identifier, error):
        super().__init__(error.errno, str(error))
        self.identifier = identifier


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


def source_track_identity(value):
    """Validate the opaque browser identity independently of its wire span ID."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode()) > 200:
        raise ProtocolError("source", "Invalid source track identity")
    return value


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
        try:
            os.chmod(path, 0o600)
            self._header()
        except BaseException:
            with suppress(OSError):
                self.file.close()
            with suppress(OSError):
                Path(path).unlink()
            raise

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


def recording_utc_offset(instant):
    """Record the site's offset at capture start, not the Library's later zone."""
    try:
        offset = instant.astimezone().utcoffset()
    except (OSError, OverflowError, ValueError):
        return None
    if offset is None or offset.total_seconds() % 60:
        return None
    minutes = int(offset.total_seconds() / 60)
    return minutes if -720 <= minutes <= 840 else None


class RecordingSession:
    def __init__(
        self,
        parent,
        title="Browser recording",
        source="chromium",
        *,
        clock=time.monotonic,
        writer_factory=WAVWriter,
        started_at=None,
    ):
        from contextlib import nullcontext

        from .library_location import capture_lease, primary, routing

        instant = datetime.now(UTC) if started_at is None else started_at
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ProtocolError("state", "Recording start must be an absolute time")
        instant = instant.astimezone(UTC)
        offset = recording_utc_offset(instant)
        self._lease_fd = None
        default_parent = parent is None
        with routing() if parent is None else nullcontext():
            if parent is None:
                from .library_publication import capture_parent

                parent = capture_parent(primary())
            self.clock = clock
            self.writer_factory = writer_factory
            identifier = str(uuid4())
            self.directory = Path(parent) / f"Recording-{identifier}"
            self.directory.mkdir(mode=0o700, parents=True)
            from .library_publication import root_for
            from .library_v2.fsutil import fsync_dir

            self.library_root = root_for(self.directory)
            if self.library_root is not None:
                fsync_dir(self.directory.parent)
            self._native_clock_origin_ns = time.monotonic_ns()
            self._native_clocks = {}
            self.manifest = {
                "version": 1,
                "id": identifier,
                "createdAt": instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "title": optional_string(title, "Browser recording", "state")[:500],
                "source": source,
                "state": "recording",
                "tracks": [],
            }
            if offset is not None:
                self.manifest["startedAtUtcOffsetMinutes"] = offset
            self.writers = {}
            self.finished = False
            self._native_io_error = None
            self.capture_record = None
            self._failed_writers = set()
            self._changing = False
            self._last_checkpoint = 0
            self._checkpoint_generation = 0
            self.checkpoint()
            self._started_at = self._last_checkpoint
            if default_parent:
                self._lease_fd = capture_lease(identifier)

    def checkpoint(self):
        if self.capture_record is not None:
            self.capture_record.finish(self.manifest)
        if self._changing:
            return
        identifiers = {track["id"] for track in self.manifest["tracks"]}
        for identifier, writer in self.writers.items():
            if identifier not in identifiers:
                continue  # A rollback can still be closing its unreferenced additions.
            if identifier in self._failed_writers:
                self._writer_failed(identifier)
                continue
            try:
                snapshot = getattr(writer, "snapshot", None)
                if snapshot:
                    snapshot()
                writer.sync()
            except OSError:
                if self.capture_record is None:
                    raise  # Legacy sessions retain their existing fatal policy.
                self._writer_failed(identifier)
        for track in self.manifest["tracks"]:
            if track.get("spans"):
                continue
            if (clock := self._native_clocks.get(track["id"])) and (anchors := clock.values()):
                track["clockAnchors"] = anchors
        path = self.directory / "session.json"
        temporary = path.with_suffix(".tmp")
        if self.capture_record is not None:
            self.capture_record.finish(self.manifest)
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

    def _writer_failed(self, identifier):
        """Retire one known WAV; manifest/directory durability is never caught."""
        first = identifier not in self._failed_writers
        self._failed_writers.add(identifier)
        self.capture_record.fail(self.manifest, identifier, io=True)
        writer = self.writers[identifier]
        track = next(t for t in self.manifest["tracks"] if t["id"] == identifier)
        track.update(frames=writer.frames, ended=True)
        if track.get("startOffsetMs") is not None:
            track["endOffsetMs"] = track["startOffsetMs"] + track["frames"] * 1000 / track["sampleRate"]
        if first:
            with suppress(OSError):
                writer.close()
        snapshot = getattr(writer, "snapshot", None)
        if snapshot:
            with suppress(OSError):
                snapshot()

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
                self._failed_writers.discard(track["id"])
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

    def add_track(
        self, identifier, label, kind, rate, channels, *, filename=None, label_source=None, source_track_id=None
    ):
        if self.finished:
            raise ProtocolError("source", "Invalid or duplicate audio source")
        label = source_parameters(identifier, label, kind, rate, channels, self.writers)
        source_track_id = source_track_identity(source_track_id)
        if label_source is not None and label_source not in ("browser", "device"):
            raise ProtocolError("source", "Invalid audio source label provenance")
        if filename is not None and filename not in ("track-microphone.wav", "track-system.wav"):
            raise ProtocolError("source", "Invalid native track filename")
        filename = filename or f"track-{uuid4()}.wav"
        try:
            writer = self.writer_factory(self.directory / filename, rate, channels)
        except OSError as error:
            if self.capture_record is not None:
                raise SourceIOError(identifier, error) from error
            raise
        if self.library_root is not None and isinstance(writer, WAVWriter):
            from .rotating_wav import RotatingWAV

            writer = RotatingWAV(self, identifier, writer)
        self.writers[identifier] = writer
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
        if source_track_id is not None:
            self.manifest["tracks"][-1]["sourceTrackId"] = source_track_id
        if self.capture_record is not None and self.manifest["tracks"][-1]["labelSource"] == "browser":
            self.capture_record.bind(identifier, source_track_id or identifier)
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
        input_frames = len(data) // (writer.channels * 2)
        if self.capture_record is not None:
            self.capture_record.check_overlap(self.manifest, identifier, writer.frames, delta + input_frames)
        previous = writer.frames
        try:
            if delta:
                before_padding = writer.frames
                try:
                    writer.silence(delta)
                finally:
                    # silence() writes bounded blocks and may fail after some
                    # have reached the WAV. Keep that absence distinct from
                    # received zero PCM even on a source-local disk failure.
                    padded = writer.frames - before_padding
                    if self.capture_record is not None and padded:
                        track["gapFrames"] = track.get("gapFrames", 0) + padded
            writer.append(data)
        except OSError as error:
            if self.capture_record is not None:
                received = min(input_frames, max(0, writer.frames - previous - delta))
                self.capture_record.admit(identifier, received)
                self.capture_record.fail(self.manifest, identifier, io=True)
                track["frames"] = writer.frames
                track["droppedFrames"] = track.get("droppedFrames", 0) + input_frames - received
                self._writer_failed(identifier)
                raise SourceIOError(identifier, error) from error
            raise
        track["frames"] = writer.frames
        if self.capture_record is not None:
            self.capture_record.admit(identifier, input_frames)
        if self.clock() - self._last_checkpoint >= 1:
            self.checkpoint()

    def append_native(self, identifier, data, offset, *, gap_frames=0, host_ns=None):
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
                    silence = getattr(writer, "silence_gap", writer.silence)
                    silence(gap_frames)
                finally:
                    written = writer.frames - before
                    if written:
                        track["gapFrames"] = track.get("gapFrames", 0) + written
                    track["frames"] = writer.frames
            if self.library_root is not None and host_ns is not None:
                from .capture_clock import ClockAnchors

                clock = self._native_clocks.get(identifier)
                if clock is None:
                    clock = self._native_clocks[identifier] = ClockAnchors(writer.rate, self._native_clock_origin_ns)
                index = writer.frames
                clock.add(index, host_ns)
                if hasattr(writer, "observe_clock"):
                    writer.observe_clock(host_ns)
            self.append(identifier, data, offset)
        except Exception as error:
            self._native_io_error = str(error)
            track["frames"] = writer.frames
            raise

    def finish(self, problem=None, *, problem_kind=None, reason=None, at_ms=None, retain_lease=False):
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
        for identifier, writer in self.writers.items():
            try:
                writer.close()
            except OSError as error:
                failure = failure or error
                if self.capture_record is not None:
                    self.capture_record.fail(self.manifest, identifier, io=True)
        for track in self.manifest["tracks"]:
            track["ended"] = True
            if track.get("startOffsetMs") is not None:
                track["endOffsetMs"] = track["startOffsetMs"] + track["frames"] * 1000 / track["sampleRate"]
        gaps = [t for t in self.manifest["tracks"] if t.get("gapFrames", 0)]
        if self.capture_record is not None and self.capture_record.finish(self.manifest):
            problem = problem or "Some announced audio was not fully captured."
            if problem_kind != "io":
                problem_kind = "io" if self.capture_record.io_failed else "capture"
        if self._failed_writers:
            problem, problem_kind = problem or "Some audio could not be saved.", "io"
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
            if not retain_lease:
                self.release_lease()
        if failure:
            raise failure

    def release_lease(self):
        """Release ownership only after publication/cancellation has finished."""
        if self._lease_fd is not None:
            os.close(self._lease_fd)
            self._lease_fd = None


class BrowserHost:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else None
        self.session = None
        self.sequences = {}
        self.stopped = False
        self.audio_sink = None
        self.source_outcomes = False
        self.capture_id = None
        self.declarations = {}
        self.ended = set()

    def handle(self, message, *, started_at=None):
        try:
            return self._handle(message, started_at=started_at)
        except (ProtocolError, SourceIOError) as error:
            if (
                self.source_outcomes
                and isinstance(error, ProtocolError)
                and error.code == "state"
                and (self.stopped or self.session is not None and self.session.finished)
            ):
                return dict(type="error", code="state", message=str(error), scope="command")
            if self.source_outcomes and message.get("type") == "sources" and isinstance(error, ProtocolError):
                return dict(type="error", code=error.code, message=str(error), scope="command")
            if not self.source_outcomes or message.get("type") not in ("track", "audio", "trackEnd"):
                raise
            identifier = message.get("id")
            retired = isinstance(identifier, str) and identifier in self.ended
            return self.refuse_source(message, error, record=not retired)

    def refuse_source(self, message, error, *, record=True):
        identifier = message.get("id")
        code = error.code if isinstance(error, ProtocolError) else "io"
        if record and self.session is not None and not self.session.finished:
            ledger = self.session.capture_record
            declaration = self.declarations.get(identifier) if isinstance(identifier, str) else None
            if declaration is not None:
                identity = declaration[3] or identifier
                ledger.seen[identity]["failed"] = True
                stored = identifier
                if self.audio_sink is not None:
                    wire = self.audio_sink.tracks.get(identifier)
                    stored = wire.recorded if wire is not None else None
                if stored in ledger.bindings:
                    ledger.fail(self.session.manifest, stored, io=code == "io")
                else:
                    ledger.io_failed |= code == "io"
                # A failed WAV is retired; the extension retries with a fresh
                # wire span. Other sources and this connection stay usable.
                if message.get("type") == "audio" or identifier not in self.sequences:
                    self.ended.add(identifier)
                    if self.audio_sink is not None and identifier in self.audio_sink.tracks:
                        self.audio_sink.end_track(identifier)
                    elif stored in ledger.bindings and stored in self.session.writers:
                        self.session.end_track(stored)
            self.session.checkpoint()
        result = dict(type="error", code=code, message=str(error), scope="source")
        if code == "state" and not record and self.session is not None and not self.session.finished:
            result["retired"] = True  # This span ended; the capture is still live.
        if isinstance(identifier, str):
            result["id"] = identifier
        return result

    def _source_selected(self, kind):
        sink = self.audio_sink
        return sink is None or sink.browser and (sink.me if kind in ("microphone", "local") else sink.others)

    def prepare_stop(self, message):
        if not self.source_outcomes:
            return None
        if message.get("captureId") != self.capture_id:
            return dict(type="error", code="state", message="Capture identity does not match", scope="command")
        if self.session.finished:
            if message.get("discoveredSources"):
                return dict(type="error", code="state", message="Capture already saved", scope="command")
            return dict(type="stopped", path=str(self.session.directory))
        try:
            self.session.capture_record.discover(message.get("discoveredSources", []), expected=self._source_selected)
        except ProtocolError as error:
            return dict(type="error", code=error.code, message=str(error), scope="command")
        return None

    def _handle(self, message, *, started_at=None):
        kind = message.get("type")
        if kind not in ("start", "track", "audio", "stop") and not (
            self.source_outcomes and kind in ("trackEnd", "sources")
        ):
            raise ProtocolError("version", "Unknown browser message type")
        if kind == "start":
            if self.session:
                raise ProtocolError("state", "A recording has already started")
            version = message.get("version")
            version = 1 if version is None else version
            if type(version) is not int or version != 1:
                raise ProtocolError("version", "Unsupported browser recording protocol version")
            title = optional_string(message.get("title"), "Browser recording", "state")
            features = message.get("features", [])
            if (
                not isinstance(features, list)
                or len(features) > 8
                or any(not isinstance(f, str) or len(f) > 64 for f in features)
                or len(set(features)) != len(features)
            ):
                raise ProtocolError("version", "Invalid recording features")
            self.session = RecordingSession(self.directory, title, started_at=started_at)
            self.source_outcomes = "source-outcomes-v1" in features
            self.capture_id = message.get("captureId")
            result = {"type": "started", "path": str(self.session.directory)}
            if self.source_outcomes:
                from .capture_record import CaptureRecord

                self.session.capture_record = CaptureRecord()
                result["features"] = ["source-outcomes-v1"]
            return result
        if kind == "stop" and self.source_outcomes and self.session is not None and self.session.finished:
            return self.prepare_stop(message)
        if self.session is None or self.stopped or self.session.finished:
            raise ProtocolError("state", "No active recording")
        identifier = message.get("id")
        if self.source_outcomes and kind in ("track", "audio", "trackEnd") and isinstance(identifier, str):
            wire = self.audio_sink.tracks.get(identifier) if self.audio_sink is not None else None
            stored = wire.recorded if wire is not None else identifier
            if identifier in self.ended or (identifier in self.declarations and stored in self.session._failed_writers):
                self.ended.add(identifier)
                raise ProtocolError("state", "Audio source has ended")
        if kind == "sources":
            sources = message.get("discoveredSources")
            if not isinstance(sources, list) or not sources:
                raise ProtocolError("source", "Invalid discovered audio sources")
            self.session.capture_record.discover(sources, expected=self._source_selected)
            self.session.checkpoint()
        elif kind == "track":
            identifier = message.get("id")
            if self.source_outcomes and isinstance(identifier, str) and identifier in self.ended:
                raise ProtocolError("state", "Audio source has ended")
            if self.source_outcomes:
                label = source_parameters(
                    identifier,
                    message.get("label"),
                    message.get("kind"),
                    message.get("sampleRate"),
                    message.get("channels"),
                    {},
                )
                root = source_track_identity(message.get("sourceTrackId"))
                declaration = (message.get("kind"), message.get("sampleRate"), message.get("channels"), root)
                if identifier in self.declarations:
                    if self.declarations[identifier] != declaration:
                        raise ProtocolError("source", "Conflicting track declaration")
                    if identifier not in self.sequences:
                        raise ProtocolError("source", "Source declaration was refused")
                    return dict(type="ack")
                self.session.capture_record.observe(
                    root or identifier, message["kind"], label, expected=self._source_selected(message["kind"])
                )
                self.declarations[identifier] = declaration
                if any(
                    other != identifier and (value[3] or other) == (root or identifier) and other not in self.ended
                    for other, value in self.declarations.items()
                ):
                    raise ProtocolError("source", "Overlapping live source spans")
            (self.audio_sink or self.session).add_track(
                identifier,
                message.get("label"),
                message.get("kind"),
                message.get("sampleRate"),
                message.get("channels"),
                source_track_id=message.get("sourceTrackId"),
            )
            self.sequences[identifier] = 0
        elif kind == "audio":
            identifier = message.get("id")
            if not isinstance(identifier, str) or identifier not in self.sequences:
                raise ProtocolError("source", "Unknown audio source")
            if identifier in self.ended:
                raise ProtocolError("source", "Audio source has ended")
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
        elif kind == "trackEnd":
            identifier = message.get("id")
            if isinstance(identifier, str) and identifier in self.ended:
                raise ProtocolError("state", "Audio source has ended")
            if not isinstance(identifier, str) or identifier not in self.sequences:
                raise ProtocolError("source", "Unknown audio source")
            if message.get("reason") not in ("ended", "replaced", "retry", "stopped", "failed", "callChanged"):
                raise ProtocolError("source", "Invalid source end reason")
            dropped = message.get("droppedFrames")
            if type(dropped) is not int or not 0 <= dropped <= 2147483629:
                raise ProtocolError("source", "Invalid dropped frame count")
            end = message.get("endOffsetMs")
            if (
                "endOffsetMs" not in message
                or end is not None
                and (type(end) not in (int, float) or not math.isfinite(end) or not 0 <= end <= 86460000)
            ):
                raise ProtocolError("source", "Invalid source end time")
            if identifier not in self.ended:
                stored = self.audio_sink.tracks[identifier].recorded if self.audio_sink is not None else identifier
                # The extension counts drops per wire track, with no position. Add
                # them to the latest span that recorded it, even after deselection,
                # without replacing the host's own write-loss count. A source that
                # was never recorded lost nothing from the file.
                counted = stored if self.audio_sink is None else self.audio_sink.tracks[identifier].last_recorded
                track = next((t for t in self.session.manifest["tracks"] if t["id"] == counted), None)
                if track is not None:
                    track["droppedFrames"] = track.get("droppedFrames", 0) + dropped
                if message["reason"] == "failed" and any(t["id"] == stored for t in self.session.manifest["tracks"]):
                    self.session.capture_record.fail(self.session.manifest, stored)
                if message["reason"] == "failed":
                    root = self.declarations[identifier][3] or identifier
                    self.session.capture_record.seen[root]["failed"] = True
                (self.audio_sink or self.session).end_track(identifier)
                self.ended.add(identifier)
                if stored is None:
                    self.session.checkpoint()
        elif kind == "stop":
            refused = self.prepare_stop(message)
            if refused is not None:
                return refused
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
                    label=_("Recovered audio"),
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
        title=_("Recovered recording"),
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
                label=_("Recovered audio"),
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
    physical_tracks = []
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("Invalid recording track")
        spans = track.get("spans")
        if spans is None:
            physical_tracks.append(track)
        elif not isinstance(spans, list) or not spans or len(spans) > 4096:
            raise ValueError("Invalid recording spans")
        else:
            physical_tracks.extend(spans)
    for track in physical_tracks:
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
    for track in tracks:
        if isinstance(track.get("spans"), list):
            track["frames"] = sum(span["frames"] for span in track["spans"])
    return manifest
