# SPDX-License-Identifier: GPL-3.0-or-later
"""A broker-owned browser recording survives the native messaging proxy's exit."""

import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

from .capture_sources import BrowserStreams, change_time, selected_pcm
from .recording_session import BrowserHost, ProtocolError
from .session_events import source_state


class BrowserCapture:
    def __init__(self, changed, *, parent=None, tap_factory=None):
        self.host = BrowserHost(parent)
        self.changed = changed
        self.tap_factory = tap_factory
        self.lock = threading.RLock()
        self.transition = threading.RLock()
        self.capture_id = None
        self.microphone = None
        self.native_taps = {}
        self.me, self.others = True, True
        self.browser_lost = False
        self.streams = None
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
                events=list(self.host.session.manifest.get("events", [])),
            )

    def start(self, message, *, others_source="browser", reason="start"):
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
                if self.finished or self.stop_requested.is_set():
                    raise ProtocolError("state", "Recording start was cancelled")
                self.capture_id, self.origin_ms = identifier, origin
                self.starting = True
                result = self.host.handle(message)
                self.host.session.manifest["createdAt"] = created
                self.me = microphone
                self.browser_lost = others_source == "system"
                self.streams = self.host.audio_sink = BrowserStreams(self.host.session, me=microphone)
                self.streams.browser = not self.browser_lost
                with self.host.session.change(
                    [
                        dict(atMs=0, type="othersSource", source=others_source, reason=reason),
                        dict(atMs=0, type="selection", me=microphone, others=True, reason="start"),
                    ]
                ):
                    mic = self._add_native("microphone") if microphone else None
                    system = self._add_native("system", 0) if self.browser_lost else None
            if mic is not None:
                self._start_native("microphone", mic)
            if system is not None:
                self._start_native("system", system)
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

    def _add_native(self, kind, at=None):
        from .strings import text

        identifier = kind + "-" + str(uuid4())
        session = self.host.session
        filename = f"track-{kind}.wav"
        if any(t["file"] == filename for t in session.manifest["tracks"]):
            filename = None
        session.add_track(
            identifier,
            text("common.me" if kind == "microphone" else "common.theOthers"),
            kind,
            48000,
            1,
            filename=filename,
            label_source="device",
        )
        if at is not None:
            session.manifest["tracks"][-1]["startOffsetMs"] = at
        return identifier

    def _start_native(self, kind, identifier, at=0):
        factory = self.tap_factory
        if factory is None:
            from .native_tap import NativeTap

            factory = NativeTap

        def append(data, offset):
            with self.lock:
                track = next(t for t in self.host.session.manifest["tracks"] if t["id"] == identifier)
                if not self.finished and not track.get("ended"):
                    selected, offset = selected_pcm(data, offset, 48000, 1, at)
                    if selected:
                        self.host.session.append(identifier, selected, offset)

        def level(value):
            with self.lock:
                selected = self.me if kind == "microphone" else self.others
                self.levels[0 if kind == "microphone" else 1] = value if selected else 0

        def failed(message):
            self.error = message
            self.changed(self, "error")

        if self.stop_requested.is_set():
            return
        tap = factory(kind, self.origin_ms, append, failed, level=level)
        self.native_taps[kind] = identifier, tap
        if kind == "microphone":
            self.microphone = tap
        tap.start()

    def select(self, me, others):
        if type(me) is not bool or type(others) is not bool or not (me or others):
            raise ValueError("Select at least one recording source")
        return self._change(me, others)

    def fallback(self, reason="lost"):
        if reason not in ("missing", "lost"):
            raise ValueError("Invalid fallback reason")
        return self._change(self.me, self.others, fallback=reason)

    def _change(self, me, others, *, fallback=None):
        try:
            return self._apply_change(me, others, fallback=fallback)
        except OSError as error:
            self.error = str(error)
            self.changed(self, "error")
            raise

    def _apply_change(self, me, others, *, fallback=None):
        if self.starting and not self.ready.wait(12):
            raise TimeoutError("Native capture did not finish starting")
        with self.transition:
            with self.lock:
                if self.finished or self.stopping or self.stop_requested.is_set():
                    raise ProtocolError("state", "No active recording")
                old = source_state(self.host.session.manifest.get("events"))
                lost = self.browser_lost or fallback is not None
                source = "system" if lost else "browser"
                events = []
                at = change_time(self.host.session, self.elapsed_ms())
                if (me, others) != (self.me, self.others):
                    events.append(dict(atMs=at, type="selection", me=me, others=others, reason="user"))
                if not others:
                    if self.others:
                        events.append(dict(atMs=at, type="othersSource", source="none", reason="user"))
                elif not self.others or (lost and old.source != "system"):
                    reason = fallback or ("lost" if lost else "user")
                    events.append(dict(atMs=at, type="othersSource", source=source, reason=reason))
                if not events:
                    self.browser_lost = lost
                    return self.snapshot()
                needed = self.streams.additions(me, others, browser=not lost)
                needed += int(me and "microphone" not in self.native_taps)
                needed += int(others and lost and "system" not in self.native_taps)
                if len(self.host.session.writers) + needed > 64:
                    raise ProtocolError("limit", "A session supports at most 64 sources")
                additions, removed = [], []
                with self.host.session.change(events):
                    self.streams.select(me, others, at, browser=not lost)
                    for kind, enabled in (("microphone", me), ("system", others and lost)):
                        current = self.native_taps.get(kind)
                        if current and not enabled:
                            self.host.session.end_track(current[0])
                            removed.append((kind, current[1]))
                        elif current is None and enabled:
                            additions.append((kind, self._add_native(kind, at)))
                self.me, self.others, self.browser_lost = me, others, lost
                for kind, _tap in removed:
                    self.native_taps.pop(kind)
                    if kind == "microphone":
                        self.microphone = None
                    self.levels[0 if kind == "microphone" else 1] = 0
            # Device state changes may wait for streaming threads. Never hold
            # the PCM writer lock, and never run this transition on GTK.
            try:
                for _kind, tap in removed:
                    tap.stop()
                for kind, identifier in additions:
                    self._start_native(kind, identifier, at)
            except Exception as error:
                self.error = str(error)
                self.changed(self, "error")
                raise
            self.changed(self, "sources")
            return self.snapshot()

    def handle(self, message):
        if message.get("type") == "stop":
            if not self.stop_requested.is_set() and (message.get("failureReason") == "lost" or self.browser_lost):
                self.fallback()
                return dict(type="stopped", path=str(self.directory))
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
            if self.host.session is None:
                self.finished = True
                self.stopped.set()
                return dict(type="stopped", path=None)
            if self.finished:
                return dict(type="stopped", path=str(self.directory))
            joining = self.stopping
            self.stopping = True
        if joining:
            if not self.stopped.wait(12):
                raise TimeoutError("Recording is still being saved")
            return dict(type="stopped", path=str(self.directory))
        try:
            with self.transition:
                for _identifier, tap in tuple(self.native_taps.values()):
                    try:
                        tap.stop()
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
            if self.stop_requested.is_set():
                self.stop()
            else:
                self.fallback()

    def elapsed_ms(self):
        return max(0.0, time.time_ns() / 1_000_000 - self.origin_ms)
