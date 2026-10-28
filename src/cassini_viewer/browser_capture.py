# SPDX-License-Identifier: GPL-3.0-or-later
"""A broker-owned browser recording survives the native messaging proxy's exit."""

import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

from .recording_session import BrowserHost, ProtocolError


class BrowserCapture:
    def __init__(self, changed, *, parent=None, tap_factory=None):
        self.host = BrowserHost(parent)
        self.changed = changed
        self.tap_factory = tap_factory
        self.lock = threading.RLock()
        self.capture_id = None
        self.microphone = None
        self.stopping = self.starting = False
        self.finished = False
        self.levels = [0.0, 0.0]
        self.error = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.stop_requested = threading.Event()
        self.origin_ms = None
        self.line = None
        self.minutes_left = 0

    @property
    def directory(self):
        return self.host.session.directory if self.host.session else None

    def snapshot(self):
        with self.lock:
            return dict(
                recording=not self.finished,
                stopping=self.stopping,
                starting=self.starting,
                captureId=self.capture_id,
                path=str(self.directory),
                levels=list(self.levels),
                line=self.line,
                minutesLeft=self.minutes_left,
                tracks=sum(
                    t["kind"] in ("remote", "tab", "media") and not t.get("ended")
                    for t in self.host.session.manifest["tracks"]
                ),
                title=self.host.session.manifest["title"],
                error=self.error,
            )

    def start(self, message):
        identifier = message.get("captureId")
        origin = message.get("timelineOriginMs")
        microphone = message.get("microphone", True)
        if (
            not isinstance(identifier, str)
            or not 0 < len(identifier) <= 128
            or type(origin) is not int
            or not 0 <= origin <= 253402300799000
            or type(microphone) is not bool
        ):
            raise ProtocolError("state", "Invalid independent capture metadata")
        created = datetime.fromtimestamp(origin / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            with self.lock:
                self.capture_id, self.origin_ms = identifier, origin
                self.starting = True
                result = self.host.handle(message)
                self.host.session.manifest["createdAt"] = created
                self.host.session.checkpoint()
            if microphone:
                self._start_microphone()
        except Exception as error:
            self.starting = False
            self.ready.set()
            if self.host.session is not None:
                self.stop(str(error))
            raise
        finally:
            self.starting = False
            self.ready.set()
        if self.stop_requested.is_set():
            self.stop()
        return result

    def _start_microphone(self):
        from .strings import text

        factory = self.tap_factory
        if factory is None:
            from .native_tap import NativeTap

            factory = NativeTap

        identifier = "microphone-" + str(uuid4())
        with self.lock:
            self.host.session.add_track(
                identifier,
                text("common.me"),
                "microphone",
                48000,
                1,
                filename="track-microphone.wav",
                label_source="device",
            )

        def append(data, offset):
            with self.lock:
                if not self.finished:
                    self.host.session.append(identifier, data, offset)

        def level(value):
            with self.lock:
                self.levels[0] = value

        def failed(message):
            self.error = message
            self.changed(self, "error")

        self.microphone = factory("microphone", self.origin_ms, append, failed, level=level)
        self.microphone.start()

    def handle(self, message):
        if message.get("type") == "stop":
            return self.stop(message.get("problem"))
        with self.lock:
            if self.finished or self.stopping:
                raise ProtocolError("state", "No active recording")
            return self.host.handle(message)

    def stop(self, problem=None):
        self.stop_requested.set()
        if self.starting and not self.ready.wait(12):
            raise TimeoutError("Native capture did not finish starting")
        with self.lock:
            if self.finished:
                return dict(type="stopped", path=str(self.directory))
            joining = self.stopping
            self.stopping = True
        if joining:
            if not self.stopped.wait(12):
                raise TimeoutError("Recording is still being saved")
            return dict(type="stopped", path=str(self.directory))
        try:
            if self.microphone is not None:
                try:
                    self.microphone.stop()
                except Exception as error:
                    problem = problem or str(error)
            with self.lock:
                return self.host.handle(dict(type="stop", problem=problem))
        except Exception as error:
            self.error = str(error)
            raise
        finally:
            with self.lock:
                self.finished = True
                self.stopping = False
                self.levels = [0.0, 0.0]
                self.stopped.set()
            self.changed(self, "finished")

    def close(self, problem="Browser disconnected before recording was stopped."):
        if not self.finished and self.host.session is not None:
            self.stop(problem)

    def elapsed_ms(self):
        return max(0.0, time.time_ns() / 1_000_000 - self.origin_ms)
