# SPDX-License-Identifier: GPL-3.0-or-later
"""A broker-owned browser recording survives the native messaging proxy's exit."""

import contextlib
import logging
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
        self.browser_lost_since = None
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
                permittedSources=sorted(
                    {
                        t["kind"]
                        for t in self.host.session.manifest["tracks"]
                        if t["kind"] in ("microphone", "system") and t["frames"] > 0
                    }
                ),
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
                self.browser_lost_since = time.monotonic() if self.browser_lost else None
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

    def _add_native(self, kind, at=None, *, identifier=None):
        from .strings import text

        identifier = identifier or kind + "-" + str(uuid4())
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

    def _make_native(self, kind, identifier, at=0, *, active=False):
        factory = self.tap_factory
        if factory is None:
            from .native_tap import NativeTap

            factory = NativeTap

        from .native_timing import NS, NativeTimeline

        state = dict(active=active, at=at, error=None, timeline=NativeTimeline(48000))

        def append(data, offset, *, host_ns=None):
            with self.lock:
                if not state["active"]:
                    return  # Readiness PCM is not a committed recording span.
                track = next(t for t in self.host.session.manifest["tracks"] if t["id"] == identifier)
                if not self.finished and not track.get("ended"):
                    original_offset = offset
                    selected, offset = selected_pcm(data, offset, 48000, 1, state["at"])
                    if selected:
                        skipped = (len(data) - len(selected)) // 2
                        stamp = round(original_offset * 1_000_000) if host_ns is None else host_ns
                        offset, gap = state["timeline"].map(stamp + skipped * NS // 48000, len(selected) // 2, offset)
                        self.host.session.append_native(identifier, selected, offset, gap_frames=gap)

        def level(value):
            with self.lock:
                if not state["active"]:
                    return
                selected = self.me if kind == "microphone" else self.others
                self.levels[0 if kind == "microphone" else 1] = value if selected else 0

        def failed(message):
            with self.lock:
                state["error"] = message
                active = (
                    state["active"]
                    and not self.finished
                    and not any(
                        track["id"] == identifier and track.get("ended")
                        for track in self.host.session.manifest["tracks"]
                    )
                )
                if active:
                    self.error = message
            if active:
                self.changed(self, "error")

        return factory(kind, self.origin_ms, append, failed, level=level), state

    def _start_native(self, kind, identifier, at=0):
        if self.stop_requested.is_set():
            return
        tap, _state = self._make_native(kind, identifier, at, active=True)
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
        return self._apply_change(me, others, fallback=fallback)

    def _events(self, me, others, lost, fallback, at):
        old = source_state(self.host.session.manifest.get("events"))
        events = []
        if (me, others) != (self.me, self.others):
            events.append(dict(atMs=at, type="selection", me=me, others=others, reason="user"))
        if not others:
            if self.others:
                events.append(dict(atMs=at, type="othersSource", source="none", reason="user"))
        elif not self.others or (lost and old.source != "system"):
            events.append(
                dict(
                    atMs=at,
                    type="othersSource",
                    source="system" if lost else "browser",
                    reason=fallback or ("lost" if lost else "user"),
                )
            )
        return events

    def _apply_change(self, me, others, *, fallback=None):
        if self.starting and not self.ready.wait(12):
            raise TimeoutError("Native capture did not finish starting")
        with self.transition:
            with self.lock:
                if self.finished or self.stopping or self.stop_requested.is_set():
                    raise ProtocolError("state", "No active recording")
                lost = self.browser_lost or fallback is not None
                if (me, others, lost) == (self.me, self.others, self.browser_lost):
                    return self.snapshot()
                desired = {kind for kind, enabled in (("microphone", me), ("system", others and lost)) if enabled}
                kinds = desired - self.native_taps.keys()
                needed = self.streams.additions(me, others, browser=not lost) + len(kinds)
                if len(self.host.session.writers) + needed > 64:
                    raise ProtocolError("limit", "A session supports at most 64 sources")
            prepared = {}
            committed = False
            try:
                # Start/read the replacements while the old source remains live.
                # No new PCM or event may reach the session until commit below.
                for kind in sorted(kinds):
                    identifier = kind + "-" + str(uuid4())
                    tap, state = self._make_native(kind, identifier)
                    prepared[kind] = identifier, tap, state
                    tap.start()
                with self.lock:
                    if self.finished or self.stopping or self.stop_requested.is_set():
                        raise ProtocolError("state", "No active recording")
                    for _identifier, _tap, state in prepared.values():
                        if state["error"]:
                            raise OSError(state["error"])
                    # New wire tracks can arrive while native taps get ready.
                    needed = self.streams.additions(me, others, browser=not lost) + len(prepared)
                    if len(self.host.session.writers) + needed > 64:
                        raise ProtocolError("limit", "A session supports at most 64 sources")
                    at = change_time(self.host.session, self.elapsed_ms())
                    removed = {kind: value for kind, value in self.native_taps.items() if kind not in desired}
                    with (
                        self.host.session.change(self._events(me, others, lost, fallback, at)),
                        self.streams.change(),
                    ):
                        self.streams.select(me, others, at, browser=not lost)
                        for identifier, _tap in removed.values():
                            self.host.session.end_track(identifier)
                        for kind, (identifier, _tap, _state) in prepared.items():
                            self._add_native(kind, at, identifier=identifier)
                    if lost and not self.browser_lost:
                        self.browser_lost_since = time.monotonic()
                    self.me, self.others, self.browser_lost = me, others, lost
                    for kind in removed:
                        self.native_taps.pop(kind)
                        self.levels[0 if kind == "microphone" else 1] = 0
                    for kind, (identifier, tap, state) in prepared.items():
                        state.update(active=True, at=at)
                        self.native_taps[kind] = identifier, tap
                    self.microphone = self.native_taps.get("microphone", (None, None))[1]
                    committed = True
                for _identifier, tap in removed.values():
                    try:
                        tap.stop()
                    except Exception:
                        logging.getLogger(__name__).exception("Could not finish a deselected source")
                self.changed(self, "sources")
                return self.snapshot()
            finally:
                if not committed:
                    for _identifier, tap, _state in prepared.values():
                        with contextlib.suppress(Exception):
                            tap.stop()

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
                try:
                    self.fallback()
                except Exception as error:
                    # Loss cannot leave an unowned microphone/session alive if
                    # limits, durability or the replacement device reject Q4.
                    self.stop(str(error))

    def elapsed_ms(self):
        return max(0.0, time.time_ns() / 1_000_000 - self.origin_ms)
