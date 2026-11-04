# SPDX-License-Identifier: GPL-3.0-or-later
"""A broker-owned browser recording survives the native messaging proxy's exit."""

import contextlib
import logging
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from .capture_sources import BrowserStreams, canonical_order, change_time, selected_pcm
from .recording_session import BrowserHost, ProtocolError, stop_reason
from .refusals import SIDES, NothingOpened, StartCancelled, entry
from .refusals import append as append_refusal
from .session_events import source_state
from .spectro_source import SpectroSource

RESUME_FEATURE = "resume-capture-v1"
# D52.3c: a resume may join a system-sound stand-in (D49, seam callConnected) or
# a system/lost fallback after its audio transport closed (D52.4, seam restored).
RESUME_TARGETS = (("system", "missing"), ("system", "lost"))
RESUME_ASKS = 8
LOG = logging.getLogger(__name__)


class ResumeRefused(ProtocolError):
    """A refused resume-capture-v1 attach. It changes nothing and is command scoped."""

    def __init__(self, message="Target is no longer active."):
        super().__init__("state", message)


def utf8_length(value):
    return len(value.encode("utf-8", "surrogatepass"))


def generation_value(value):
    """A browser-generated call/document generation, or None."""
    return value if isinstance(value, str) and 0 < utf8_length(value) <= 200 else None


class BrowserCapture:
    def __init__(self, changed, *, parent=None, tap_factory=None):
        self.host = BrowserHost(parent)
        self.changed = changed
        self.tap_factory = tap_factory
        self.lock = threading.RLock()
        self.transition = threading.RLock()
        self.capture_id = None
        self.call_generation = None
        # The tab a control status last bound this capture to, under any peer
        # (instance-less companions have no call_captures binding).
        self.call_tab = None
        self.microphone = None
        self.native_taps = {}
        # Native sides requested at start that never opened (R79 §1). A
        # fallback or restore does not reopen them; turning one off forgets it.
        self.refused_sides = set()
        self.discarded = False  # D58: nothing opened, and the folder was removed.
        # R82 1b: unanswered resume asks, {requestId: atMs}, oldest first; one per
        # open control connection that asked, so a small bound is enough.
        self.resume_asks = {}
        self.me, self.others = True, True
        self.browser_lost = False
        self.browser_lost_since = None
        # D52 "Return before the fallback commits": a fallback decided but not
        # committed (its device has not admitted a first buffer). The recording
        # keeps its written othersSource meanwhile (SPEC.md); a return cancels it.
        self.fallback_pending = False
        self._fallback_cancelled = False
        # D52.5: each transition into a browser loss is a new loss, so a
        # resume is asked again once per loss and control connection.
        self.loss_serial = 0
        # The loss whose restore the 64-track format cap refused. It stays on
        # computer sound for that loss (SPEC-CONTROL: keep the computer source
        # if a staged restoration fails), and policy stops asking each tick.
        self.restore_refused = None
        # The recording tab's documentGeneration, and the last declaration
        # made while a status still confirmed it (see observe_document).
        self.document_generation = None
        self.document_mark = 0
        # D61: the documentGenerations seen leave. None ever leaves the set.
        self.departed_documents = set()
        self.audio_connected = False
        self.lifecycle_capable = False
        self.multistream_capable = False
        self.tab_closed = False
        self.end_reason = None
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
        self._pcm_stopped = threading.Event()
        self._publication_cancel = threading.Event()
        self._publication_future = None
        self.publication = None
        self._mute_observer = None
        self.output_muted = False
        self.spectrogram = SpectroSource()
        self._spectro_starts = {}

    @property
    def directory(self):
        return self.host.session.directory if self.host.session and not self.discarded else None

    def snapshot(self):
        with self.lock:
            return dict(
                recording=not self.finished and not self.stopping,
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
                outputMuted=self.output_muted,
                minutesLeft=self.minutes_left,
                tracks=sum(
                    t["kind"] in ("remote", "tab", "media") and not t.get("ended")
                    for t in self.host.session.manifest["tracks"]
                ),
                title=self.host.session.manifest["title"],
                error=self.error,
                events=list(self.host.session.manifest.get("events", [])),
            )

    def start(self, message, *, others_source="browser", reason="start", refusals=()):
        """Start this capture. `refusals` are start refusals made before the folder
        existed (a rejected browser Start): they are stored with the start events."""
        identifier = message.get("captureId")
        origin = message.get("timelineOriginMs")
        microphone = message.get("microphone", True)
        generation = message.get("callGeneration")
        if (
            not isinstance(identifier, str)
            or not 0 < len(identifier) <= 128
            or type(origin) is not int
            or not 0 <= origin <= 253402300799000
            or type(microphone) is not bool
            or generation is not None
            and (not isinstance(generation, str) or not 0 < len(generation.encode("utf-8")) <= 200)
        ):
            raise ProtocolError("state", "Invalid independent capture metadata")
        instant = datetime.fromtimestamp(origin / 1000, UTC)
        created = instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            with self.lock:
                if self.finished or self.stop_requested.is_set():
                    raise ProtocolError("state", "Recording start was cancelled")
                self.capture_id, self.origin_ms = identifier, origin
                self.call_generation = generation
                self.audio_connected = others_source == "browser"
                self.starting = True
                result = self.host.handle(message, started_at=instant)
                self.host.session.manifest["createdAt"] = created
                self.me = microphone
                self.browser_lost = others_source == "system"
                self.browser_lost_since = time.monotonic() if self.browser_lost else None
                # Independent capture records Me natively (captureLocal: false).
                self.streams = self.host.audio_sink = BrowserStreams(
                    self.host.session, me=microphone, picture_enabled=lambda: self.spectrogram.active, native_me=True
                )
                self.streams.browser = not self.browser_lost
                # The start selection records what was requested; a native
                # side's track is added only when that side opens.
                # A browser refusal explains this othersSource: one checkpoint.
                with self.host.session.change(
                    [
                        dict(atMs=0, type="othersSource", source=others_source, reason=reason),
                        dict(atMs=0, type="selection", me=microphone, others=True, reason="start"),
                    ]
                ):
                    for item in refusals:
                        append_refusal(self.host.session.manifest, item)
                self._sync_spectrogram()
            requested = [kind for kind, wanted in (("microphone", microphone), ("system", self.browser_lost)) if wanted]
            refused = self._open_natives(requested)
            items = [self._refusal(0, kind, refused[kind]) for kind in requested if kind in refused]
            if requested and self.browser_lost and not any(kind in self.native_taps for kind in requested):
                if len(refused) < len(requested):
                    # A Stop cancelled a side before it opened, and nothing
                    # opened: no recording, and no refusal (R79 §1). The blank
                    # folder goes with no banner, as on the native path.
                    raise StartCancelled("Recording start was cancelled")
                # Nothing records (D58 point 2): no folder, only the start banner.
                error = NothingOpened(str(refused[requested[0]]), [*refusals, *items])
                error.unwritten = items  # The browser refusals are already in the start checkpoint.
                raise error from refused[requested[0]]
            if items:
                with self.lock:
                    if not self.finished:
                        self.host.session.refuse(*items)
            if self.tap_factory is None:
                from .output_mute import OutputMuteObserver

                self._mute_observer = OutputMuteObserver(
                    self.directory, self._output_sample, self._output_changed, validate=self._output_validate
                )
                self._mute_observer.start()
        except Exception as error:
            # Finish before ready: a Stop waiting on this start must find it
            # finished, not finish the folder this start discards.
            self.starting = False
            if self.host.session is not None:
                if isinstance(error, StartCancelled):
                    self.stop(discard=())
                else:
                    self.stop(str(error), discard=error.unwritten if isinstance(error, NothingOpened) else None)
            self.ready.set()
            raise
        finally:
            self.starting = False
            self.ready.set()
        if self.stop_requested.is_set():
            self.stop()
        return result

    @staticmethod
    def _classify(kind, error):
        """The D58 code of a native side that never opened.

        D58.1: the system tap's explicit error keeps its own code
        (permissionDenied only when the system reported it). The microphone
        keeps deviceUnavailable.
        """
        code = getattr(error, "refusal_code", None) if kind == "system" else None
        return code if code in ("permissionDenied", "deviceUnavailable") else "deviceUnavailable", str(error)

    def _refusal(self, at_ms, kind, error):
        code, message = self._classify(kind, error)
        return entry(at_ms, SIDES[kind], code, message=message)

    def _output_sample(self):
        from .output_mute import OutputSample, monitor_device

        with self.lock:
            identifier, tap = self.native_taps.get("system", (None, None))
            if not self._has_track(identifier):
                identifier = None  # D58.1: armed, no track until its first buffer.
            session = self.host.session
            at_ms = self.elapsed_ms()
        monitor = monitor_device(tap.pipeline if tap else None)
        return OutputSample(identifier, monitor, at_ms, (session, tap))

    def _output_validate(self, sample):
        with self.lock:
            session, tap = sample.token
            identifier, current = self.native_taps.get("system", (None, None))
            if not self._has_track(identifier):
                identifier = None
            if (
                session is self.host.session
                and tap is current
                and sample.track == identifier
                and not self.stopping
                and not self.finished
            ):
                return self.elapsed_ms()
        return None

    def _output_changed(self, muted):
        with self.lock:
            changed = self.output_muted != muted
            self.output_muted = muted
        if changed:
            self.changed(self, "output-mute")

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

    def _has_track(self, identifier):
        """Whether a committed native tap has its track (D58.1: an armed system
        tap gets it at its first buffer). Lock held."""
        return identifier is not None and any(t["id"] == identifier for t in self.host.session.manifest["tracks"])

    def _make_native(self, kind, identifier, at=0, *, active=False):
        factory = self.tap_factory
        if factory is None:
            from .native_tap import NativeTap

            factory = NativeTap

        from .native_timing import NS, NativeTimeline

        # heard: a first buffer arrived. track: the committed side has its track.
        # refuse_at: when an explicit error before a first buffer refuses an
        # armed tap (D58.1); None where it is not a refusal (a fallback).
        state = dict(active=active, at=at, error=None, timeline=NativeTimeline(48000), heard=False, track=active)
        state["refuse_at"] = None

        def append(data, offset, *, host_ns=None):
            picture = None
            opened = False
            with self.lock:
                state["heard"] = True
                if not state["active"]:
                    return  # Readiness PCM is not a committed recording span.
                if not state["track"]:
                    # D58.1: an armed tap opens on its first buffer, however late.
                    if self.finished or self.stop_requested.is_set():
                        return
                    with self.host.session.change([]):
                        self._add_native(kind, max(state["at"], offset), identifier=identifier)
                        canonical_order(self.host.session.manifest["tracks"])
                    state["track"] = opened = True
                    self._sync_spectrogram()
                track = next(t for t in self.host.session.manifest["tracks"] if t["id"] == identifier)
                if not self.finished and not self.stop_requested.is_set() and not track.get("ended"):
                    original_offset = offset
                    selected, offset = selected_pcm(data, offset, 48000, 1, state["at"])
                    if selected:
                        skipped = (len(data) - len(selected)) // 2
                        stamp = round(original_offset * 1_000_000) if host_ns is None else host_ns
                        offset, gap = state["timeline"].map(stamp + skipped * NS // 48000, len(selected) // 2, offset)
                        self.host.session.append_native(
                            identifier,
                            selected,
                            offset,
                            gap_frames=gap,
                            host_ns=host_ns + skipped * NS // 48000 if host_ns is not None else None,
                        )
                        start = self._spectro_starts.get(identifier)
                        if start is None:
                            start = (track.get("spans") or [track])[0].get("startOffsetMs")
                            self._spectro_starts[identifier] = start
                        if self.spectrogram.active:
                            picture = dict(
                                identifier=identifier,
                                data=selected,
                                offset_ms=offset,
                                rate=48000,
                                channels=1,
                                kind=kind,
                                start_ms=start,
                                gap_frames=gap,
                                level=self.levels[0 if kind == "microphone" else 1],
                            )
            if picture is not None:
                self.spectrogram.observe(**picture, now_ms=self.elapsed_ms())
            else:
                self.spectrogram.release_if_idle()
            if opened:
                LOG.info("Native %s side (%s) opened on its first buffer", SIDES[kind], kind)
                if self._mute_observer:
                    self._mute_observer.refresh()
                self.changed(self, "sources")

        def level(value):
            with self.lock:
                if not state["active"]:
                    return
                selected = self.me if kind == "microphone" else self.others
                self.levels[0 if kind == "microphone" else 1] = value if selected else 0

        def failed(message):
            refused = None
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
                if active and not state["track"] and state["refuse_at"] is not None:
                    # D58.1: an armed tap that never heard a buffer did not open:
                    # its explicit error is a refusal, and the recording goes on.
                    active = False
                    if not self.stopping and not self.stop_requested.is_set():
                        state["active"] = False
                        if self.native_taps.get(kind, (None,))[0] == identifier:
                            self.native_taps.pop(kind)
                            self.refused_sides.add(kind)
                        refused = self._refusal(state["refuse_at"], kind, self._tap_error(state, message))
                        self.host.session.refuse(refused)
                elif active:
                    self.error = message
                    self.end_reason = "error"
            if refused is not None:
                LOG.warning("Native %s side (%s) did not open: %s", SIDES[kind], kind, message)
                tap = state.get("tap")
                if tap is not None:
                    # Not on the tap's own message thread, which stop() joins.
                    threading.Thread(
                        target=self._stop_tap, args=(tap,), name="cassini-refused-tap", daemon=True
                    ).start()
                self.changed(self, "sources")
            if active:
                self.changed(self, "error")

        tap = factory(kind, self.origin_ms, append, failed, level=level)
        state["tap"] = tap
        return tap, state

    @staticmethod
    def _tap_error(state, message):
        from .native_tap import CaptureError

        return CaptureError(message, getattr(state.get("tap"), "refusal_code", "deviceUnavailable"))

    @staticmethod
    def _stop_tap(tap):
        try:
            tap.stop()
        except Exception:
            LOG.debug("A refused tap did not stop cleanly", exc_info=True)

    def _open_natives(self, kinds):
        """Open each requested native side on its own and in parallel (R79 §1).

        A side opens when its tap admits a first buffer within its 3 s bound
        (NativeTap.start), and commits at once. D58.1: the system tap has no
        such bound; silent after the window, it commits armed and gets its
        track at its first buffer. Returns the refused sides' errors; a commit
        failure is not a refusal and propagates.
        """
        if not kinds:
            return {}
        with ThreadPoolExecutor(len(kinds), thread_name_prefix="cassini-native-open") as pool:
            futures = {kind: pool.submit(self._open_native, kind) for kind in kinds}
        refused = {kind: error for kind, future in futures.items() if (error := future.result()) is not None}
        with self.lock:
            self.refused_sides.update(refused)
        for kind, error in refused.items():
            LOG.warning("Native %s side (%s) did not open at start: %s", SIDES[kind], kind, error)
        return refused

    def _open_native(self, kind):
        """Open-then-commit one side, as a turn-on does. Returns its refusal, if any."""
        if self.stop_requested.is_set():
            return None
        identifier = kind + "-" + str(uuid4())
        tap, state = self._make_native(kind, identifier)
        try:
            tap.start()
        except Exception as error:
            return error
        committed = False
        try:
            with self.lock:
                if self.finished or self.stop_requested.is_set():
                    return None
                if state["error"]:
                    return OSError(state["error"])
                at = change_time(self.host.session, self.elapsed_ms())
                if state["heard"]:
                    # One checkpoint holds the track with its start.
                    with self.host.session.change([]):
                        self._add_native(kind, at, identifier=identifier)
                        canonical_order(self.host.session.manifest["tracks"])
                else:
                    # D58.1: the system tap heard nothing yet. It is armed, not
                    # refused; its track starts at its first buffer.
                    LOG.info("Native %s side (%s) heard nothing yet; it opens on its first buffer", SIDES[kind], kind)
                state.update(active=True, at=at, track=state["heard"], refuse_at=0)
                self.native_taps[kind] = identifier, tap
                if kind == "microphone":
                    self.microphone = tap
                self._sync_spectrogram()
                committed = True
            return None
        finally:
            if not committed:
                with contextlib.suppress(Exception):
                    tap.stop()

    def select(self, me, others, *, retry=False):
        """Change what is recorded. `retry` (R82 1c) also reopens a selected side
        that was refused, as Try again does; it writes no selection event."""
        if type(me) is not bool or type(others) is not bool or not (me or others):
            raise ValueError("Select at least one recording source")
        return self._change(me, others, retry=retry)

    def fallback(self, reason="lost"):
        if reason not in ("missing", "lost"):
            raise ValueError("Invalid fallback reason")
        return self._change(self.me, self.others, fallback=reason)

    @property
    def browser_ready(self):
        return self.others and self.browser_ready_since()

    def browser_ready_since(self, since=None, *, recorded=False):
        with self.lock:
            if recorded and (self.browser_lost or not self.others):
                return False
            now = time.monotonic()
            return self.audio_connected and any(
                track.kind not in ("microphone", "local")
                and track.available
                and (not recorded or track.recorded is not None)
                and (received := track.recorded_at if recorded else track.received_at) is not None
                and (since is None or received >= since)
                and now - received <= 2
                for track in self.streams.tracks.values()
            )

    def remote_open(self):
        """Whether a browser track that has delivered PCM is still open.

        Its recorder posts frames, silent ones included, until it ends
        (SPEC.md: declared, not yet ended by trackEnd or stop, has delivered
        PCM). That holds on a fallback too (D57): a frozen companion keeps an
        attached transport whose spans stopped, and the watchdog closes it. A
        navigated or reloaded frame's spans never get trackEnd; document
        retirement marks them dormant for good, and a closed transport's spans
        are ended, so neither arms it.
        """
        with self.lock:
            return any(
                track.kind not in ("microphone", "local")
                and not track.ended
                and not self.dormant(track)
                and track.received_at is not None
                for track in self.streams.tracks.values()
            )

    def dormant(self, track):
        """Whether a span no longer arms the watchdog, evaluated at every check.

        D61: a span that carries a documentGeneration is dormant exactly while
        that generation has departed; the declaration-order rules of
        observe_document apply only to spans without one.
        """
        if track.document is not None:
            return track.document in self.departed_documents
        return track.dormant

    def observe_document(self, generation=None, *, navigating=False, present=False):
        """Retire a gone document's spans from the stale-PCM watchdog.

        T1, navigating: every current remote span belongs to the document
        that is going away. The old document keeps running until the next one
        commits and can still declare spans (an obsolete frame's packets are
        still forwarded), so a departing window opens: spans declared in it
        are born dormant. It closes when the tab is present again, which the
        extension publishes as soon as the next document's frame connects and
        before that frame is told to start recording, or when a status names
        a different documentGeneration (a status still on the departing
        document, closed or otherwise, keeps it open).
        T2, a new documentGeneration (a reload the app did not see as
        navigating): only spans declared up to the last status that still
        confirmed the old generation are retired. A new document's span can
        reach the broker before the status that names its generation, and it
        must stay armed. sourceTrackId is opaque (SPEC.md), so a span's own
        document is never inferred from it.
        D61: a span that names its documentGeneration needs no window. The
        old generation departs on T1 and T2, and so does every open span's
        own (on T2, every one but the new document's): a late span of a gone
        document stays dormant after the window closes. Only status (and
        meetings) rows move the current generation, never a track.
        Returns how many spans were retired.
        """
        with self.lock:
            if self.finished or self.streams is None:
                return 0
            generation = generation_value(generation)
            changed = generation is not None and generation != self.document_generation
            retired = 0
            if changed and self.document_generation is not None:
                retired = self._retire_spans(self.document_mark)
                retired += self._depart(self.document_generation, keep=generation)
            if generation is not None:
                # Any reported generation, the departing one's on navigating
                # included, is the latest confirmation of what was declared.
                self.document_generation = generation
                self.document_mark = self.streams.declared
            if navigating:
                retired += self._retire_spans(None)
                retired += self._depart(self.document_generation)
                self.streams.departing = True
            elif present or changed:
                # The next document is here; the same one never closes it.
                self.streams.departing = False
            return retired

    def _retire_spans(self, through):
        retired = 0
        for track in self.streams.tracks.values():
            if (
                track.kind not in ("microphone", "local")
                and track.document is None
                and not track.ended
                and not track.dormant
                and (through is None or track.serial <= through)
            ):
                track.dormant = True
                retired += 1
        return retired

    def _depart(self, generation, *, keep=None):
        """D61: `generation` and every open span's own (but `keep`) have departed."""
        spans = [
            track
            for track in self.streams.tracks.values()
            if track.document is not None and not track.ended and not self.dormant(track)
        ]
        if generation is not None:
            self.departed_documents.add(generation)
        self.departed_documents.update(track.document for track in spans if track.document != keep)
        return sum(self.dormant(track) for track in spans)

    def retire_transport_spans(self, identifiers):
        """End the open spans a closed audio transport declared.

        Their recorders went with the transport, and no trackEnd can arrive
        for them any more. A resumed transport re-declares the same page
        sources (one sourceTrackId per MediaStreamTrack root for the whole
        document) under fresh span ids, which the host would otherwise refuse
        as overlapping live spans. Retiring is not a source failure: the
        ledger's failed flag is left alone.
        """
        with self.lock:
            session = self.host.session
            if (
                self.finished
                or self.stopping
                or self.stop_requested.is_set()
                or self.streams is None
                or session is None
                or session.finished
            ):
                return 0
            retired = 0
            for identifier in identifiers:
                track = self.streams.tracks.get(identifier)
                if track is None or track.ended or identifier in self.host.ended:
                    continue
                self.host.ended.add(identifier)
                self.streams.end_track(identifier)
                retired += 1
            if retired:
                self._sync_spectrogram()
            return retired

    def resume(self, message):
        """R3/R4: attach a new audio transport to this recording, or refuse.

        Validation shares Stop's critical section: Stop sets stop_requested
        under the capture lock, and a source change in progress holds the
        transition lock, so a committing fallback is waited out, not misread.
        A refusal changes nothing. On success nothing but the attachment
        changes: no folder, clock, microphone, selection or title. The source
        switch happens later, at the ordinary restore commit, once the
        resumed transport has delivered PCM.
        """
        with self.transition, self.lock:
            session = self.host.session
            if (
                self.finished
                or self.stopping
                or self.stop_requested.is_set()
                or self.starting
                or session is None
                or session.finished
                or self.streams is None
            ):
                raise ResumeRefused()
            if (
                message.get("captureId") != self.capture_id
                or message.get("timelineOriginMs") != self.origin_ms
                or type(message.get("timelineOriginMs")) is not int
                or self.call_generation is None
                or message.get("callGeneration") != self.call_generation
                or self.tab_closed
            ):
                raise ResumeRefused()
            current = source_state(session.manifest.get("events"))
            # The recorded source is the authority: system/missing or system/lost
            # (browser_lost with others selected), never browser or none.
            if (current.source, current.reason) not in RESUME_TARGETS:
                raise ResumeRefused()
            if self.audio_connected:
                # Another transport is still attached: never two on one capture.
                raise ResumeRefused()
            self.audio_connected = True
            self.resume_asks.clear()  # Attached: every ask is answered.
            return dict(
                type="started",
                path=str(self.directory),
                features=(["source-outcomes-v1"] if self.host.source_outcomes else []) + [RESUME_FEATURE],
                resumed=True,
                captureId=self.capture_id,
                timelineOriginMs=self.origin_ms,
                callGeneration=self.call_generation,
            )

    def mark_resume_ask(self, request_id):
        """R82 1b: this recording asked the browser to resume it (one entry per ask)."""
        with self.lock:
            self.resume_asks.pop(request_id, None)
            self.resume_asks[request_id] = self.elapsed_ms()
            while len(self.resume_asks) > RESUME_ASKS:
                self.resume_asks.pop(next(iter(self.resume_asks)))

    def clear_resume_ask(self, request_id=None):
        """Forget one ask, or every ask without a requestId."""
        with self.lock:
            if request_id is None:
                self.resume_asks.clear()
            else:
                self.resume_asks.pop(request_id, None)

    def refuse_resume(self, packet, message, *, request_id=None):
        """D58 #10 (R82 1b): a refused resume is startRejected/resumeRefused here.

        One entry per ask: the native host's refusal (on the audio pipe) or the
        browser's ok:false reply, whichever comes first, writes it at the ask's
        time and forgets that ask; the other writes nothing. Both answer only
        the requestId they carry; a native refusal without a known one writes
        nothing (its ask's own reply records it). Only while this recording
        still waits on system sound for that call: live (not stopping, stopped
        or starting), its others from a resume target, no transport attached,
        and a native resume naming its call generation. A refusal never changes
        the recording itself.
        """
        with self.lock:
            session = self.host.session
            if not isinstance(request_id, str):
                return
            at = self.resume_asks.get(request_id)
            if at is None:
                return
            if (
                session is None
                or session.finished
                or self.finished
                or self.stopping
                or self.starting
                or self.stop_requested.is_set()
                or self.audio_connected
                or self.tab_closed
                or self.call_generation is None
                or packet is not None
                and packet.get("callGeneration") != self.call_generation
            ):
                return
            current = source_state(session.manifest.get("events"))
            if (current.source, current.reason) not in RESUME_TARGETS:
                return
            del self.resume_asks[request_id]
            item = entry(at, "browser", "startRejected", detail="resumeRefused", message=message)
            try:
                session.refuse(item)
            except Exception:
                LOG.exception("Could not record a refused resume")

    def cancel_fallback(self):
        """A return before the fallback commits: drop it, writing nothing.

        No system track and no restored event: the written othersSource never
        changed, and the missing Others are already absent between their spans
        (D47, D52.2). Never waits for the device the fallback is opening.
        """
        with self.lock:
            if not self.fallback_pending:
                return False
            self._fallback_cancelled = True
        return True

    def restore(self):
        if self.cancel_fallback():
            return self.snapshot()
        try:
            return self._apply_change(self.me, self.others, restore=True)
        except ProtocolError as error:
            if error.code != "limit":
                raise
            with self.lock:
                first = self.restore_refused != self.loss_serial
                self.restore_refused = self.loss_serial
            if first:
                logging.getLogger(__name__).warning(
                    "Keeping computer sound for recording %s; the browser source cannot be restored: %s",
                    self.capture_id,
                    error,
                )
            return self.snapshot()

    @property
    def restore_blocked(self):
        """Whether this loss's restore already failed on the track limit."""
        return self.restore_refused is not None and self.restore_refused == self.loss_serial

    def _change(self, me, others, *, fallback=None, retry=False):
        return self._apply_change(me, others, fallback=fallback, retry=retry)

    def _events(self, me, others, lost, fallback, at):
        old = source_state(self.host.session.manifest.get("events"))
        events = []
        if (me, others) != (self.me, self.others):
            events.append(dict(atMs=at, type="selection", me=me, others=others, reason="user"))
        if not others:
            if self.others:
                events.append(dict(atMs=at, type="othersSource", source="none", reason="user"))
        elif not lost and old.fallback:
            events.append(
                dict(
                    atMs=at,
                    type="othersSource",
                    source="browser",
                    reason="callConnected" if old.reason == "missing" else "restored",
                )
            )
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

    def _apply_change(self, me, others, *, fallback=None, restore=False, retry=False):
        if self.starting and not self.ready.wait(12):
            raise TimeoutError("Native capture did not finish starting")
        with self.transition:
            with self.lock:
                if self.finished or self.stopping or self.stop_requested.is_set():
                    raise ProtocolError("state", "No active recording")
                if self.tab_closed and (fallback is not None or restore):
                    return self.snapshot()
                if restore and (not self.browser_lost or not self.browser_ready):
                    return self.snapshot()
                lost = False if restore else self.browser_lost or fallback is not None
                desired = {kind for kind, enabled in (("microphone", me), ("system", others and lost)) if enabled}
                retrying = desired & self.refused_sides - self.native_taps.keys() if retry else set()
                if (me, others, lost) == (self.me, self.others, self.browser_lost) and not retrying:
                    return self.snapshot()
                kinds = (desired - self.native_taps.keys() - self.refused_sides) | retrying
                if not (kinds or desired & self.native_taps.keys() or others and not lost):
                    # Every side this change keeps was refused at start. Recording
                    # nothing is never committed: the change asks for them again,
                    # as a turn-on, and fails like one if they still refuse.
                    kinds = desired - self.native_taps.keys()
                needed = self.streams.additions(me, others, browser=not lost) + len(kinds)
                if len(self.host.session.writers) + needed > 64:
                    raise ProtocolError("limit", "A session supports at most 64 sources")
                pending = fallback is not None and not self.browser_lost
                if pending:
                    self.fallback_pending, self._fallback_cancelled = True, False
            prepared = {}
            committed = False
            opening = None
            try:
                # Start/read the replacements while the old source remains live.
                # No new PCM or event may reach the session until commit below.
                for kind in sorted(kinds):
                    opening = kind
                    identifier = kind + "-" + str(uuid4())
                    tap, state = self._make_native(kind, identifier)
                    prepared[kind] = identifier, tap, state
                    tap.start()
                opening = None
                with self.lock:
                    if self.finished or self.stopping or self.stop_requested.is_set():
                        raise ProtocolError("state", "No active recording")
                    if self.tab_closed and (fallback is not None or restore):
                        return self.snapshot()  # A tab may close while a fallback device gets ready.
                    if pending and self._fallback_cancelled:
                        return self.snapshot()  # The call returned first: nothing is committed.
                    for kind, (_identifier, _tap, state) in prepared.items():
                        if state["error"]:
                            opening = kind
                            raise OSError(state["error"])
                    # New wire tracks can arrive while native taps get ready.
                    needed = self.streams.additions(me, others, browser=not lost) + len(prepared)
                    if len(self.host.session.writers) + needed > 64:
                        raise ProtocolError("limit", "A session supports at most 64 sources")
                    at = change_time(self.host.session, self.elapsed_ms())
                    removed = {kind: value for kind, value in self.native_taps.items() if kind not in desired}
                    # D58.1: an armed tap (no first buffer yet) has no track to end.
                    ending = {kind: value for kind, value in removed.items() if self._has_track(value[0])}
                    with (
                        self.streams.change(),
                        self.host.session.change(self._events(me, others, lost, fallback, at)),
                    ):
                        self.streams.select(me, others, at, browser=not lost)
                        for identifier, _tap in ending.values():
                            self.host.session.end_track(identifier)
                        for kind, (identifier, _tap, state) in prepared.items():
                            if state["heard"]:
                                self._add_native(kind, at, identifier=identifier)
                    self._sync_spectrogram()
                    if lost and not self.browser_lost:
                        self.browser_lost_since = time.monotonic()
                        self.loss_serial += 1
                    self.me, self.others, self.browser_lost = me, others, lost
                    if not lost:
                        self.browser_lost_since = None
                    for kind in removed:
                        self.native_taps.pop(kind)
                        self.levels[0 if kind == "microphone" else 1] = 0
                    for kind, (identifier, tap, state) in prepared.items():
                        # A turn-on's armed tap is refused at its time; a
                        # fallback's or restore's failure is not a refusal.
                        turn_on = fallback is None and not restore
                        state.update(active=True, at=at, track=state["heard"], refuse_at=at if turn_on else None)
                        self.native_taps[kind] = identifier, tap
                    self.microphone = self.native_taps.get("microphone", (None, None))[1]
                    self.refused_sides = (self.refused_sides & desired) - prepared.keys()
                    committed = True
                if "system" in ending and self._mute_observer:
                    self._mute_observer.retire(ending["system"][0], at)
                for _identifier, tap in removed.values():
                    try:
                        tap.stop()
                    except Exception:
                        logging.getLogger(__name__).exception("Could not finish a deselected source")
                if self._mute_observer:
                    self._mute_observer.refresh()
                self.changed(self, "sources")
                return self.snapshot()
            except Exception as error:
                if opening is not None and fallback is None and not restore:
                    self._turn_on_refused(opening, error)
                raise
            finally:
                if pending:
                    with self.lock:
                        self.fallback_pending = self._fallback_cancelled = False
                if not committed:
                    for _identifier, tap, _state in prepared.values():
                        with contextlib.suppress(Exception):
                            tap.stop()

    def _turn_on_refused(self, kind, error):
        """D58 (R79 §6.6): a user's refused turn-on is appended at its real time.

        It writes no selection event and leaves the selection unchanged. A
        fallback or restore that cannot open its device is not a turn-on.
        """
        item = self._refusal(self.elapsed_ms(), kind, error)
        LOG.warning("Native %s side (%s) did not open when turned on: %s", SIDES[kind], kind, error)
        with self.lock:
            if self.finished or self.stopping or self.host.session is None or self.host.session.finished:
                return
            try:
                self.host.session.refuse(item)
            except Exception:
                LOG.exception("Could not record a refused turn-on")

    def _sync_spectrogram(self):
        """Declare committed spans before accepting their PCM; no DSP here."""
        for track in self.host.session.manifest["tracks"]:
            identifier = track["id"]
            if track.get("ended"):
                self.spectrogram.retire(identifier)
                self._spectro_starts.pop(identifier, None)
            elif identifier not in self.spectrogram.known:
                writer = self.host.session.writers[identifier]
                self.spectrogram.declare(identifier, kind=track["kind"], rate=writer.rate, now_ms=self.elapsed_ms())
                spans = track.get("spans") or [track]
                self._spectro_starts[identifier] = spans[0].get("startOffsetMs")

    def handle(self, message):
        if message.get("type") == "stop":
            # Source-summary admission reads the current selection and merges
            # accounting. Serialize it with PCM/source changes; device shutdown
            # below still happens outside the writer lock.
            with self.lock:
                refused = self.host.prepare_stop(message)
            if refused is not None:
                return refused
            reason = stop_reason(message)
            if not self.stop_requested.is_set() and message.get("failureReason") == "lost":
                self.fallback()
                return dict(type="stopped", path=str(self.directory))
            if reason == "user":
                self.user_stopped = True
            return self.stop(message.get("problem"), reason=reason)
        with self.lock:
            if self.finished or self.stopping or self.stop_requested.is_set():
                if self.host.source_outcomes and message.get("type") == "sources":
                    return dict(type="error", code="state", message="Capture already saved", scope="command")
                if self.host.source_outcomes and message.get("type") in ("track", "audio", "trackEnd"):
                    return self.host.refuse_source(message, ProtocolError("state", "No active recording"), record=False)
                raise ProtocolError("state", "No active recording")
            self.streams.picture = None
            result = self.host.handle(message)
            if message.get("type") == "audio" and result.get("type") == "ack":
                self.audio_connected = True
            picture, self.streams.picture = self.streams.picture, None
            if message.get("type") in ("track", "trackEnd") or (
                picture is not None and picture["identifier"] not in self.spectrogram.known
            ):
                self._sync_spectrogram()
        if picture is not None:
            self.spectrogram.observe(**picture, now_ms=self.elapsed_ms())
        else:
            self.spectrogram.release_if_idle()
        return result

    def stop(self, problem=None, *, reason=None, discard=None):
        """Finish this capture. `discard` (the start refusals) removes a folder
        nothing opened into, unless the user already typed into it (D58)."""
        from .session_events import END_REASONS

        if reason is not None and (not isinstance(reason, str) or reason not in END_REASONS):
            raise ProtocolError("state", "Invalid recording end reason")
        if reason == "appQuit":
            self._publication_cancel.set()
            if self._publication_future is not None:
                self._publication_future.cancel()
        with self.lock:
            self.end_reason = self.end_reason or reason or ("error" if problem else "user")
            self.stop_requested.set()
            self.resume_asks.clear()  # A result after Stop writes nothing.
        if self.starting and not self.ready.wait(12):
            raise TimeoutError("Native capture did not finish starting")
        with self.lock:
            if self.host.session is None:
                self.finished = True
                self.stopped.set()
                return dict(type="stopped", path=None)
            if self.finished:
                # A discarded start has no folder (D58).
                return dict(type="stopped", path=None if self.directory is None else str(self.directory))
            joining = self.stopping
            self.stopping = True
        if joining:
            if not self._pcm_stopped.wait(12):
                raise TimeoutError("Recording is still being saved")
            return dict(type="stopped", path=None if self.directory is None else str(self.directory))
        publishing = False
        try:
            with self.transition:
                if self._mute_observer:
                    self._mute_observer.close(self.elapsed_ms())
                    self._mute_observer = None
                for _identifier, tap in tuple(self.native_taps.values()):
                    try:
                        tap.stop()
                    except Exception as error:
                        problem = problem or str(error)
                        self.end_reason = "error"
                with self.lock:
                    if discard is not None:
                        self.discarded = self.host.session.discard()
                        if not self.discarded:
                            self.host.session.refuse(*discard)  # A folder kept for typed content.
                    if not self.discarded:
                        self.host.session.finish(
                            problem, reason=self.end_reason, at_ms=self.elapsed_ms(), retain_lease=True
                        )
                    self.host.stopped = True
                self.spectrogram.finish()
            if self.discarded:
                return dict(type="stopped", path=None)
            from .library_publication import root_for, submit_stopped

            if root_for(self.directory) is not None:
                self._publication_future = submit_stopped(self.host.session, cancel=self._publication_cancel)
                if self._publication_cancel.is_set():
                    self._publication_future.cancel()
                publishing = True
                self._publication_future.add_done_callback(self._published)
            return dict(type="stopped", path=str(self.directory))
        except Exception as error:
            self.error = str(error)
            raise
        finally:
            self._pcm_stopped.set()
            if not publishing:
                self._finish_stop()

    def _published(self, future):
        try:
            self.publication = future.result()
        except CancelledError:
            pass  # Completed PCM remains available for startup publication.
        except Exception as error:
            self.error = str(error)
        finally:
            self._finish_stop()

    def _finish_stop(self):
        with self.lock:
            self.host.session.release_lease()
            self.finished = True
            self.stopping = False
            self.levels = [0.0, 0.0]
            self.stopped.set()
        self.changed(self, "finished")

    def close(self, problem="Browser disconnected before recording was stopped."):
        self.audio_connected = False
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
