# SPDX-License-Identifier: GPL-3.0-or-later
"""Separate native PCM tracks on one GStreamer clock, checkpointed off GTK."""

import contextlib
import logging
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from gi.repository import GLib, GObject, Gst

from .capture_sources import canonical_order, change_time, selected_pcm
from .native_timing import NS, NativeTimeline
from .recorder import request_eos
from .recording_details import ACMeter, space_left
from .recording_session import RecordingSession
from .spectro_source import SpectroSource

RATE = 48000
# A source's input stamps can never lead the pipeline clock; see _input_pts.
FUTURE_PTS_SLACK_NS = 10 * NS
WRAP_TOLERANCE_NS = 2 * NS
# R79 §1: a side has opened when its first buffer is admitted within this bound.
OPEN_TIMEOUT_S = 3
START_FAILURE = "Could not start audio capture. Check your audio devices and GStreamer plugins."
# A failed state change reports no cause of its own (no bus error came with it).
STATE_FAILURE = "Could not start native audio capture"
NO_SAMPLE = "Native audio capture did not produce a sample"
SIDES = {"microphone": "me", "system": "others"}
LOG = logging.getLogger(__name__)


def pulse_latency(sink):
    """(actual-latency-time in us, rate) of this branch's pulsesrc, or None.

    GstAudioBaseSrc reports one ring-buffer segment, floored to microseconds;
    it is also the source's latency (a segment fills before it is read).
    """
    branch = sink.get_parent()
    source = branch.get_by_name("capture-source") if branch is not None else None
    caps = source.get_static_pad("src").get_current_caps() if source is not None else None
    if caps is None:
        return None
    found, rate = caps.get_structure(0).get_int("rate")
    latency_us = source.get_property("actual-latency-time")
    return (latency_us, rate) if found and rate > 0 and latency_us > 0 else None


def segment_wrap_ns(latency_us, rate):
    """Exact stamp offset of 2^32 ring-buffer segments.

    None when 2^32 segments are not a whole number of nanoseconds (e.g. 1024
    samples at 48 kHz): a rounded shift would be wrong.
    """
    samples = -(-latency_us * rate // 1_000_000)
    if samples * 1_000_000 // rate != latency_us:
        return None
    wrap, remainder = divmod(2**32 * samples * NS, rate)
    return None if remainder else wrap


class NativeRecorder(GObject.Object):
    __gsignals__ = {
        "started": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "finished": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.pipeline = None
        self.session = None
        self.stopping = False
        self.starting = False
        self.levels = [0.0, 0.0]
        self._start_us = GLib.get_monotonic_time()
        self.line = None
        self.minutes_left = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cassini-native-io")
        self._lock = threading.Lock()
        self._finishing = False
        self._problem = None
        self.end_reason = None
        self._timeout = 0
        self._disk_timer = 0
        self._disk_future = None
        self._changing_sources = False
        self._track_ids = {"microphone": "microphone", "system": "system"}
        self._boundaries = {"microphone": 0, "system": 0}
        self._branches = {}
        self._pending_sources = {}
        self._retired_branches = set()
        self._timelines = {}
        self._pts_shifts = {}
        self._ac_meters = {}
        self.spectrogram = SpectroSource()
        self._spectro_starts = {}
        self._publication_cancel = threading.Event()
        self._publication_future = None
        self.publication = None
        self.publication_error = None
        self._mute_observer = None
        self.output_muted = False
        self._open_wake = threading.Event()
        # D58: the start's refusals, for the start banner when nothing opened.
        self.refusals = []
        self._discard = self.discarded = False
        # The start's open phase reads these; tests replace them with a fake clock.
        self._open_clock = time.monotonic
        self._open_wait = threading.Event.wait

    @property
    def activity(self):
        if self.session is None or not (self.pipeline is not None or self.starting or self.stopping or self._finishing):
            return {}
        return {self.session.manifest["id"]: "saving" if self.stopping else "recording"}

    def start(self, parent=None, *, test=False):
        if self.pipeline is not None or self.starting or self.stopping or self._changing_sources or self._finishing:
            raise RuntimeError("Already recording")
        self.starting = True
        self._start_us = GLib.get_monotonic_time()
        self._stop_requested = False
        self.session = None
        self.stopping = self._finishing = False
        self._problem = self.line = self.end_reason = None
        self.levels = [0.0, 0.0]
        # A side gets its track, WAV and boundary only once it opens (_open).
        self._track_ids = {}
        self._boundaries = {}
        self._branches = {}
        self._pending_sources = {}
        self._retired_branches = set()
        self._timelines = {}
        self._pts_shifts = {}
        self._ac_meters = {}
        self.spectrogram = SpectroSource()
        self._spectro_starts = {}
        self._publication_cancel.clear()
        self._publication_future = None
        self.publication = None
        self.publication_error = None
        self._mute_observer = None
        self.output_muted = False
        self._open_wake = threading.Event()
        self.refusals = []
        self._discard = self.discarded = False
        future = self._executor.submit(self._prepare, Path(parent) if parent is not None else None)
        future.add_done_callback(lambda f: GLib.idle_add(self._prepared, f, test))

    @staticmethod
    def _prepare(parent):
        session = RecordingSession(parent, source="gnome", title="")
        try:
            # The start selection records what was requested; a side's track
            # is added only when that side opens.
            with session.change(
                [
                    dict(atMs=0, type="othersSource", source="system", reason="missing"),
                    dict(atMs=0, type="selection", me=True, others=True, reason="start"),
                ]
            ):
                pass
            return session, space_left(session.directory)
        except Exception as error:
            session.finish(str(error), reason="error")
            raise

    def _prepared(self, future, test):
        try:
            self.session, (self.minutes_left, reserve) = future.result()
            if self._stop_requested:
                # Stopped before any side opened: the blank folder goes (D58).
                self.starting = False
                self._discard = True
                self._begin_finish()
                return False
            self._test_sources = test
            self.pipeline = Gst.Pipeline.new(None)
            # Device clocks reset independently when Pulse streams start. Use
            # one monotonic clock for both sources and their running-time/base
            # mapping; keep the audio source's normal clock-slaving behaviour.
            self.pipeline.use_clock(Gst.SystemClock.obtain())
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            self._bus_handler = bus.connect("message", self._message)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError(START_FAILURE)
            self._start_us = GLib.get_monotonic_time()
            opening = self._executor.submit(self._open, ("microphone", "system"))
            opening.add_done_callback(lambda f: GLib.idle_add(self._opened, f, test, reserve))
        except Exception as error:
            self.starting = False
            if self.session is not None:
                self._begin_finish(str(error))
            else:
                self.emit("error", str(error))
                self.emit("finished", "")
        return False

    def _open(self, kinds):
        """Open each requested side on its own and in parallel (R79 §1).

        This is _select's open-then-commit for every side at start. Each side
        changes state on its own thread and has its own deadline, taken before
        its state change. It opens when its first buffer is admitted before
        that deadline: _sample then commits its track, WAV and boundary at
        once and records that buffer, whatever the other side is doing.
        Before that, a failed state change, a bus error or no sample refuses
        only that side. Returns the committed kinds and the refused sides'
        errors; a Stop returns at once, without waiting on a state change.
        """
        pipeline, wake = self.pipeline, self._open_wake
        attempts, refused = {}, {}
        try:
            for kind in kinds:
                branch = self._branch(kind)
                attempt = dict(
                    branch=branch,
                    ready=threading.Event(),
                    error=None,
                    wake=wake,
                    pipeline=pipeline,
                    committed=False,
                    settled=False,
                    retired=False,
                    message=None,
                )
                attempts[kind] = attempt
                sink = branch.get_by_name(kind)
                if sink is not None:
                    pad = sink.get_static_pad("sink")
                    attempt["hold"] = pad, pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._hold_eos)
                attempt["deadline"] = self._open_clock() + OPEN_TIMEOUT_S
                with self._lock:
                    self._pending_sources[kind] = attempt
                pipeline.add(branch)
                threading.Thread(
                    target=self._change_side_state, args=(kind, attempt), name=f"cassini-open-{kind}", daemon=True
                ).start()
            undecided = set(attempts)
            while undecided and not self._start_cancelled(pipeline):
                wake.clear()
                with self._lock:
                    now = self._open_clock()
                    for kind in tuple(undecided):
                        attempt = attempts[kind]
                        if not attempt["committed"] and not attempt["error"] and now >= attempt["deadline"]:
                            attempt["error"] = attempt["message"] = NO_SAMPLE
                        if attempt["error"]:
                            refused[kind] = attempt["error"]
                        if attempt["committed"] or attempt["error"]:
                            undecided.discard(kind)
                if undecided and not self._start_cancelled(pipeline):
                    self._open_wait(wake, min(attempts[kind]["deadline"] for kind in undecided) - now)
        finally:
            for kind, attempt in attempts.items():
                branch = attempt["branch"]
                with self._lock:
                    if attempt["committed"]:
                        continue
                    attempt["retired"] = True
                    settled = attempt["settled"]
                    self._retired_branches.add(branch)
                    self._pending_sources.pop(kind, None)
                # A state change still under way keeps its branch's state lock:
                # detach the branch now, and its own thread stops it (below).
                if settled:
                    branch.set_state(Gst.State.NULL)
                if branch.get_parent() == pipeline:
                    pipeline.remove(branch)
        # A side can commit after the last pass, until a Stop is seen: its
        # attempt, not the pass, says it opened.
        committed = [kind for kind in kinds if attempts.get(kind, {}).get("committed")]
        for kind, error in refused.items():
            LOG.warning("Native %s side (%s) did not open at start: %s", SIDES[kind], kind, error)
        # D58: start refusals use atMs 0, in the order the sides are requested.
        self.refusals = [self._refusal(0, kind, attempts[kind]) for kind in kinds if kind in refused]
        if committed and self.refusals:
            with self._lock:
                if not self._start_cancelled(pipeline):
                    self.session.refuse(*self.refusals)
        return committed, refused

    def _change_side_state(self, kind, attempt):
        """One side's state change, off the start's wait and the other side's."""
        ok = attempt["branch"].sync_state_with_parent()
        with self._lock:
            attempt["settled"] = True
            retired = attempt["retired"]
            if not ok and not attempt["committed"] and not attempt["error"]:
                attempt["error"] = STATE_FAILURE
                attempt["message"] = attempt["message"] or STATE_FAILURE
            failed_committed = not ok and attempt["committed"]
            # ready means decided: a state change that worked decides nothing.
            if attempt["error"]:
                self._signal(attempt)
            else:
                attempt["wake"].set()
        if retired:
            attempt["branch"].set_state(Gst.State.NULL)
        elif failed_committed:
            GLib.idle_add(self._begin_finish, STATE_FAILURE)

    def _refusal(self, at_ms, kind, attempt):
        """The refusal a side that never opened records: GNOME knows only deviceUnavailable."""
        from .refusals import entry

        code, message = self._classify(kind, attempt)
        return entry(at_ms, SIDES[kind], code, message=message)

    @staticmethod
    def _classify(_kind, attempt):
        return "deviceUnavailable", attempt.get("message") or attempt.get("error")

    @staticmethod
    def _hold_eos(_pad, info):
        # A failed source ends its stream. Before its side opens that is a
        # refusal: its EOS must not complete the pipeline's EOS while the
        # other side is still opening, which would read as a finished recording.
        return Gst.PadProbeReturn.DROP if info.get_event().type == Gst.EventType.EOS else Gst.PadProbeReturn.OK

    def _start_cancelled(self, pipeline):
        return self._stop_requested or self._finishing or self.pipeline is not pipeline

    def _admit(self, kind, attempt, pts_ms):
        """A pending side's buffer, under the lock: True when it is recorded.

        A start side commits on its first buffer stamped before its deadline;
        the stamp is taken here, when the buffer is admitted. A buffer at or
        after the deadline refuses the side. A turn-on (_select) only signals.
        """
        if "deadline" not in attempt:
            self._signal(attempt)
            return False
        if attempt["error"] or attempt["retired"] or self._start_cancelled(attempt["pipeline"]):
            return False
        if self._open_clock() >= attempt["deadline"]:
            attempt["error"] = attempt["message"] = NO_SAMPLE
        else:
            self._commit_side(kind, attempt, pts_ms)
        self._signal(attempt)
        return attempt["committed"]

    @staticmethod
    def _elapsed_ms(pipeline):
        """Session time on the pipeline clock; the shared vectors replay it."""
        clock = pipeline.get_clock()
        return max(0, (clock.get_time() - pipeline.get_base_time()) / Gst.MSECOND) if clock is not None else 0

    def _commit_side(self, kind, attempt, at_ms):
        """Commit a start side at its first admitted buffer (lock held)."""
        from .strings import text

        # The track starts at its first buffer. The other side's frames do
        # not move it: change_time's overlap rule is for spans of one source.
        at = max(at_ms, *(e["atMs"] for e in self.session.manifest.get("events", [])))
        label = text("common.me" if kind == "microphone" else "common.theOthers")
        # One checkpoint holds the track with its start.
        with self.session.change([]):
            self.session.add_track(kind, label, kind, RATE, 1, filename=f"track-{kind}.wav")
            self.session.manifest["tracks"][-1]["startOffsetMs"] = at
            canonical_order(self.session.manifest["tracks"])
        self.spectrogram.declare(kind, kind=kind, now_ms=at)
        self._spectro_starts[kind] = at
        self._track_ids[kind] = kind
        self._boundaries[kind] = at
        self._branches[kind] = attempt["branch"]
        self._pending_sources.pop(kind)
        attempt["committed"] = True
        if "hold" in attempt:
            pad, probe = attempt["hold"]
            pad.remove_probe(probe)

    def _opened(self, future, test, reserve):
        self.starting = False
        if self._finishing:
            return False  # A committed side already failed; its finish reports it.
        try:
            committed, refused = future.result()
            if not committed:
                if self._stop_requested:
                    # Stopped before any side opened: no recording either. The
                    # blank folder goes (unless typed into), with no banner and
                    # no refusals: the user cancelled this start.
                    self.refusals = []
                    self._discard = True
                    self._begin_finish()
                    return False
                # Nothing opened (D58 point 2): no recording. The folder is
                # removed unless the user already typed into it; the start
                # banner shows the refusals. Keep the device's own cause.
                causes = [refused[kind] for kind in SIDES if refused.get(kind, STATE_FAILURE) != STATE_FAILURE]
                self._discard = True
                self._begin_finish(causes[0] if causes else START_FAILURE)
                return False
            if self.minutes_left < 10:
                self.line = "errors.diskLow.line"
            if not test:
                from .output_mute import OutputMuteObserver

                self._mute_observer = OutputMuteObserver(
                    self.session.directory,
                    self._output_sample,
                    lambda muted, owner=self.session: self._output_changed(owner, muted),
                    validate=self._output_validate,
                )
                self._mute_observer.start()
            self._disk_timer = GLib.timeout_add_seconds(1, self._check_disk)
            self.emit("started", str(self.session.directory))
            self.emit("changed")
            if reserve:
                self.line = "errors.diskStopped.line"
                self.end_reason = "disk"
                self.stop()
            elif self._stop_requested:
                self.stop()
        except Exception as error:
            self._begin_finish(str(error))
        return False

    def _branch(self, kind):
        device = "@DEFAULT_SOURCE@" if kind == "microphone" else "@DEFAULT_MONITOR@"
        source = (
            f"audiotestsrc is-live=true freq={440 if kind == 'microphone' else 880}"
            if self._test_sources
            else f'pulsesrc name=capture-source device={device} client-name="Cassini Recording" provide-clock=false'
        )
        # A staged live sink must not start a new pipeline-wide preroll: that
        # would pause the confirmed sources while the replacement is tested.
        branch = Gst.parse_bin_from_description(
            source + " ! queue ! audioconvert ! audioresample ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
            f"appsink name={kind} emit-signals=true sync=false async=false max-buffers=8 drop=false",
            False,
        )
        branch.get_by_name(kind).connect("new-sample", self._sample, kind)
        return branch

    def _output_sample(self):
        from .output_mute import OutputSample, monitor_device

        with self._lock:
            track = self._track_ids.get("system")
            branch, session = self._branches.get("system"), self.session
            at_ms = max(0, (GLib.get_monotonic_time() - self._start_us) // 1000)
        monitor = monitor_device(branch)
        return OutputSample(track, monitor, at_ms, (session, branch))

    def _output_validate(self, sample):
        with self._lock:
            session, branch = sample.token
            if (
                session is self.session
                and branch is self._branches.get("system")
                and sample.track == self._track_ids.get("system")
                and self.pipeline is not None
                and not self.stopping
                and not self._finishing
            ):
                return max(0, (GLib.get_monotonic_time() - self._start_us) // 1000)
        return None

    def _output_changed(self, owner, muted):
        with self._lock:
            changed = self.session is owner and self.output_muted != muted
            if changed:
                self.output_muted = muted
        if changed:
            GLib.idle_add(self.emit, "changed")

    def select(self, me, others):
        if type(me) is not bool or type(others) is not bool or not (me or others):
            raise ValueError("Select at least one recording source")
        if self.pipeline is None or self.starting or self.stopping or self._finishing or self._changing_sources:
            raise ValueError("No source change is available")
        self._changing_sources = True
        future = self._executor.submit(self._select, me, others)
        future.add_done_callback(lambda f: GLib.idle_add(self._sources_changed, f))
        return future

    def _select(self, me, others):
        from .session_events import source_state
        from .strings import text

        if self._stop_requested:
            raise ValueError("No source change is available")
        desired = {kind for kind, enabled in (("microphone", me), ("system", others)) if enabled}
        removed, added = self._track_ids.keys() - desired, desired - self._track_ids.keys()
        # A side refused at start stays selected without a track; turning it
        # off still records the choice.
        current = source_state(self.session.manifest.get("events"))
        if not removed and not added and (current.me, current.others) == (me, others):
            return current
        pipeline = self.pipeline
        prepared = {}
        committed = False
        opening = None
        try:
            for kind in sorted(added):
                opening = kind
                branch = self._branch(kind)
                attempt = dict(branch=branch, ready=threading.Event(), error=None, message=None)
                prepared[kind] = attempt
                with self._lock:
                    self._pending_sources[kind] = attempt
                pipeline.add(branch)
                if not branch.sync_state_with_parent():
                    attempt["message"] = attempt["message"] or STATE_FAILURE
                    raise OSError(STATE_FAILURE)
                if not attempt["ready"].wait(3):
                    attempt["message"] = attempt["message"] or NO_SAMPLE
                    raise OSError(NO_SAMPLE)
                if attempt["error"]:
                    raise OSError(attempt["error"])
            opening = None
            elapsed = self._elapsed_ms(pipeline)
            with self._lock:
                if self.pipeline is not pipeline or self.stopping or self._finishing or self._stop_requested:
                    raise ValueError("No source change is available")
                for kind, attempt in prepared.items():
                    if attempt["error"]:
                        opening = kind
                        raise OSError(attempt["error"])
                at = change_time(self.session, elapsed)
                events = [dict(atMs=at, type="selection", me=me, others=others, reason="user")]
                if not others and current.others:
                    events.append(dict(atMs=at, type="othersSource", source="none", reason="user"))
                elif "system" in added:
                    events.append(dict(atMs=at, type="othersSource", source="system", reason="user"))
                identifiers = dict(self._track_ids)
                retired_ids = [identifiers[kind] for kind in removed]
                retired_system = identifiers.get("system") if "system" in removed else None
                with self.session.change(events):
                    for kind in removed:
                        self.session.end_track(identifiers.pop(kind))
                    for kind in sorted(added):
                        identifier = kind + "-" + str(uuid4())
                        self.session.add_track(
                            identifier, text("common.me" if kind == "microphone" else "common.theOthers"), kind, RATE, 1
                        )
                        self.session.manifest["tracks"][-1]["startOffsetMs"] = at
                        identifiers[kind] = identifier
                for kind in added:
                    self.spectrogram.declare(identifiers[kind], kind=kind, now_ms=at)
                    self._spectro_starts[identifiers[kind]] = at
                self._track_ids = identifiers
                retired = [self._branches.pop(kind) for kind in removed]
                self._retired_branches.update(retired)
                for kind, attempt in prepared.items():
                    self._boundaries[kind] = at
                    self._branches[kind] = attempt["branch"]
                    self._pending_sources.pop(kind)
                committed = True
            for identifier in retired_ids:
                self.spectrogram.retire(identifier)
                self._spectro_starts.pop(identifier, None)
            if retired_system is not None and self._mute_observer:
                self._mute_observer.retire(retired_system, at)
            for kind in removed:
                self.levels[0 if kind == "microphone" else 1] = 0
            for branch in retired:
                branch.set_state(Gst.State.NULL)
                pipeline.remove(branch)
            return source_state(self.session.manifest.get("events"))
        except OSError:
            if opening in prepared:
                self._turn_on_refused(pipeline, opening, prepared[opening])
            raise
        finally:
            if not committed:
                for attempt in prepared.values():
                    branch = attempt["branch"]
                    with self._lock:
                        self._retired_branches.add(branch)
                    branch.set_state(Gst.State.NULL)
                    if branch.get_parent() == pipeline:
                        pipeline.remove(branch)
                with self._lock:
                    for kind in prepared:
                        self._pending_sources.pop(kind, None)

    def _turn_on_refused(self, pipeline, kind, attempt):
        """D58 (R79 §6.6): a refused turn-on is appended at its real time.

        It writes no selection event and leaves the selection unchanged.
        """
        item = self._refusal(self._elapsed_ms(pipeline), kind, attempt)
        LOG.warning("Native %s side (%s) did not open when turned on: %s", SIDES[kind], kind, item.get("message"))
        with self._lock:
            if self.pipeline is not pipeline or self._finishing or self.session.finished:
                return
            try:
                self.session.refuse(item)
            except Exception:
                LOG.exception("Could not record a refused turn-on")

    def _sources_changed(self, future):
        self._changing_sources = False
        if self._mute_observer:
            self._mute_observer.refresh()
        # Keep the old source; the caller displays the catalog failure.
        with contextlib.suppress(Exception):
            future.result()
        self.emit("changed")
        if self._stop_requested and not self.stopping and not self._finishing:
            self.stop()
        return False

    def _input_pts(self, sink, identifier, pts, frames):
        """(stamp in pipeline running time, shift removed) on one per-branch timeline.

        GstAudioBaseSrc's skew slaving resyncs the first buffer with unsigned
        segment arithmetic. When the device has already written past the
        running-time segment, the negative difference is truncated to 32 bits
        and every later stamp of that source lands exactly 2^32 segments
        (about 497 days at 10 ms) in the future. Remove that constant once per
        branch, so consecutive stamps still detect real gaps.
        """
        pipeline = self.pipeline
        if pipeline is None:
            return pts, 0, None
        cached = self._pts_shifts.get(sink)
        if cached is None:
            clock = pipeline.get_clock()
            if clock is None:
                return pts, 0, None
            now = clock.get_time() - pipeline.get_base_time()
            cached = self._pts_shifts[sink] = self._pts_shift(sink, identifier, pts, frames, now)
        shift, path = cached
        return pts - shift, shift, path

    @staticmethod
    def _pts_shift(sink, identifier, pts, frames, now):
        """(shift in ns, D57 clockSlaving path or None for an in-range stamp)."""
        if pts <= now + FUTURE_PTS_SLACK_NS:
            return 0, None
        latency = pulse_latency(sink)
        wrap = segment_wrap_ns(*latency) if latency else None
        if wrap is not None and abs(pts - now - wrap) <= WRAP_TOLERANCE_NS:
            shift, path, how = wrap, "exactWrap", "removed the exact 2^32-segment wrap"
        else:
            # Unknown cause: this buffer began one source latency before it
            # reached the appsink; without pulsesrc facts, use its duration.
            before = latency[0] * 1000 if latency else frames * NS // RATE
            shift, path, how = pts - max(0, now - before), "runningTime", f"re-anchored at running time - {before} ns"
        LOG.warning(
            "Native %s input stamp %d ns leads running time %d ns (2^32-segment wrap: %s ns); %s, shift %d ns",
            identifier,
            pts,
            now,
            wrap,
            how,
            shift,
        )
        # The wrapped segment counter keeps segment_skew near -2^32, so this
        # source never resyncs to the pipeline clock again: its stamps are
        # sample counts, not clock observations, and are never saved as anchors.
        LOG.warning(
            "Native %s input has no GStreamer drift slaving for the rest of this recording; "
            "no clock anchors are saved for it",
            identifier,
        )
        return shift, path

    def _sample(self, sink, identifier):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        raw, shift, path = buffer.pts, 0, None
        try:
            if raw == Gst.CLOCK_TIME_NONE:
                raise ValueError("Audio source has no clock timestamp")
            data = buffer.extract_dup(0, buffer.get_size())
            pts, shift, path = self._input_pts(sink, identifier, raw, len(data) // 2)
            picture = None
            with self._lock:
                attempt = self._pending_sources.get(identifier)
                if attempt is not None and not self._admit(identifier, attempt, pts / Gst.MSECOND):
                    return Gst.FlowReturn.OK  # Uncommitted readiness PCM is discarded.
                stored = self._track_ids.get(identifier)
                if stored is not None:
                    writer = self.session.writers[stored]
                    rate, channels = writer.rate, writer.channels
                    meter = self._ac_meters.setdefault(stored, ACMeter(rate, channels))
                    if meter.feed(data):
                        self.levels[0 if identifier == "microphone" else 1] = meter.level
                        GLib.idle_add(self.emit, "changed")
                    original_frames = len(data) // (channels * 2)
                    data, offset = selected_pcm(data, pts / Gst.MSECOND, rate, channels, self._boundaries[identifier])
                    if data:
                        frames = len(data) // (channels * 2)
                        host_ns = pts + (original_frames - frames) * NS // rate
                        timeline = self._timelines.setdefault(stored, NativeTimeline(rate))
                        offset, gap = timeline.map(host_ns, frames, offset)
                        # A shifted source has no clock observations (see _pts_shift).
                        absolute = None
                        if path is not None:
                            self.session.clock_slaving_lost(stored, path, shift)
                        elif self.pipeline is not None:
                            absolute = self.pipeline.get_base_time() + host_ns
                        self.session.append_native(stored, data, offset, gap_frames=gap, host_ns=absolute)
                        start = self._spectro_starts.get(stored)
                        if start is None:
                            record = next(t for t in self.session.manifest["tracks"] if t["id"] == stored)
                            start = (record.get("spans") or [record])[0].get("startOffsetMs")
                            self._spectro_starts[stored] = start
                        source = self.spectrogram
                        if source.active:
                            picture = (source, stored, data, offset, rate, channels, start, gap)
            if picture is not None:
                source, stored, data, offset, rate, channels, start, gap = picture
                source.observe(
                    stored,
                    data,
                    offset,
                    rate=rate,
                    channels=channels,
                    kind=identifier,
                    start_ms=start,
                    gap_frames=gap,
                    now_ms=(GLib.get_monotonic_time() - self._start_us) / 1000,
                    level=self.levels[0 if identifier == "microphone" else 1],
                )
            else:
                self.spectrogram.release_if_idle()
        except Exception as error:
            LOG.warning(
                "Native %s capture failed at raw input stamp %s ns (shift %d ns): %s", identifier, raw, shift, error
            )
            with self._lock:
                attempt = self._pending_sources.get(identifier)
                if attempt is not None:
                    attempt["error"] = attempt["error"] or str(error)
                    attempt["message"] = attempt.get("message") or str(error)
                    self._signal(attempt)
                else:
                    GLib.idle_add(self._begin_finish, str(error))
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    @staticmethod
    def _signal(attempt):
        attempt["ready"].set()
        if attempt.get("wake") is not None:
            attempt["wake"].set()

    def _message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            problem = error.message
            with self._lock:
                for attempt in self._pending_sources.values():
                    branch = attempt["branch"]
                    if message.src == branch or message.src.has_as_ancestor(branch):
                        # The first error names the cause; a stream error follows it.
                        if not attempt["error"]:
                            from .refusals import gst_message

                            attempt["error"], attempt["message"] = problem, gst_message(error, debug)
                        self._signal(attempt)
                        return
                if any(message.src == b or message.src.has_as_ancestor(b) for b in self._retired_branches):
                    return  # A failed/deselected device must not stop the old source.
            self._begin_finish(problem)
        elif message.type == Gst.MessageType.EOS:
            self._begin_finish()

    def stop(self):
        self.end_reason = self.end_reason or "user"
        if self.starting:
            self._stop_requested = True
            self._open_wake.set()
            return
        if self._changing_sources:
            self._stop_requested = True
            return
        if self.pipeline is None or self.stopping:
            return
        self.stopping = True
        self.emit("changed")
        self._timeout = GLib.timeout_add_seconds(10, self._timed_out)
        pipeline = self.pipeline

        # Pulse can wait forever for the first frame during EOS/preroll. Do
        # not put that event ahead of the NULL transition that unlocks its
        # read. Retire only empty branches; healthy queues still drain by EOS.
        # Classify off GTK too: a writer may currently hold the sample lock.
        def retire():
            with self._lock:
                empty = [
                    branch
                    for kind, branch in self._branches.items()
                    if self.session.writers[self._track_ids[kind]].frames == 0
                ]
                self._retired_branches.update(empty)
            for branch in empty:
                branch.set_state(Gst.State.NULL)
                pipeline.remove(branch)
            return len(empty) == len(self._branches)

        future = self._executor.submit(retire)
        future.add_done_callback(lambda f: GLib.idle_add(self._empty_sources_stopped, f, pipeline))

    def _empty_sources_stopped(self, future, pipeline):
        if self.pipeline is not pipeline or self._finishing:
            return False
        try:
            if future.result():
                self._begin_finish()
            else:
                request_eos(pipeline, self._eos_rejected)
        except Exception as error:
            self._begin_finish(str(error))
        return False

    def _eos_rejected(self, pipeline):
        if self.pipeline is pipeline and self.stopping:
            self._begin_finish("Could not finish the recording.")
        return False

    def _timed_out(self):
        self._timeout = 0
        self._begin_finish("Timed out while saving the recording.")
        return False

    def _begin_finish(self, problem=None):
        if self._finishing or (self.session is not None and self.session.finished):
            return False
        self._finishing = True
        self.stopping = True
        self._open_wake.set()
        self._problem = problem
        self.emit("changed")
        for timer in (self._timeout, self._disk_timer):
            if timer:
                GLib.source_remove(timer)
        self._timeout = self._disk_timer = 0
        pipeline, session = self.pipeline, self.session
        reason = "error" if problem else (self.end_reason or "user")
        at_ms = max(0, (GLib.get_monotonic_time() - self._start_us) / 1000)

        def finish():
            from .library_publication import root_for, submit_stopped

            if self._mute_observer:
                self._mute_observer.close(at_ms)
                self._mute_observer = None
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            try:
                if self._discard:
                    with self._lock:
                        self.discarded = session.discard()
                        if not self.discarded:
                            session.refuse(*self.refusals)  # A folder kept for typed content.
                    if self.discarded:
                        self.spectrogram.finish()
                        return
                try:
                    with self._lock:
                        session.finish(problem, reason=reason, at_ms=at_ms, retain_lease=True)
                finally:
                    self.spectrogram.finish()
                # Encoding never holds the PCM lock or GTK main context.
                if root_for(session.directory) is not None:
                    self._publication_future = submit_stopped(session, cancel=self._publication_cancel)
                    if self._publication_cancel.is_set():
                        self._publication_future.cancel()
                    try:
                        self.publication = self._publication_future.result()
                    except CancelledError:
                        raise
                    except Exception as error:
                        self.publication_error = error
                        raise
            except CancelledError:
                pass  # Completed WAVs and queue intent are recovered next launch.
            finally:
                session.release_lease()

        future = self._executor.submit(finish)
        future.add_done_callback(lambda f: GLib.idle_add(self._finished, f, pipeline))
        return False

    def _finished(self, future, pipeline):
        problem = self._problem
        try:
            future.result()
        except Exception as error:
            problem = str(error)
        if pipeline is not None:
            bus = pipeline.get_bus()
            if hasattr(self, "_bus_handler"):
                bus.disconnect(self._bus_handler)
                bus.remove_signal_watch()
                del self._bus_handler
        self.pipeline = None
        self._retired_branches.clear()
        self.stopping = self._finishing = False
        self.levels = [0.0, 0.0]
        self.emit("changed")
        if problem:
            self.emit("error", problem)
        self.emit("finished", "" if self.discarded else str(self.session.directory))
        return False

    def _check_disk(self):
        if self.pipeline is None or self.stopping:
            self._disk_timer = 0
            return False
        if self._disk_future is None:
            self._disk_future = self._executor.submit(space_left, self.session.directory)
            self._disk_future.add_done_callback(lambda f: GLib.idle_add(self._disk_checked, f))
        return True

    def _disk_checked(self, future):
        self._disk_future = None
        if self.pipeline is None or self.stopping:
            return False
        try:
            self.minutes_left, reserve = future.result()
        except OSError as error:
            return self._begin_finish(str(error))
        if reserve:
            self.line = "errors.diskStopped.line"
            self.end_reason = "disk"
            self.stop()
        elif self.minutes_left < 30:
            self.line = "errors.diskLow.line"
        elif self.line == "errors.diskLow.line":
            self.line = None
        self.emit("changed")
        return False

    def close(self):
        """Release this finished session's IO worker without joining GTK."""
        if self.pipeline is not None or self.starting or self.stopping or self._finishing:
            raise RuntimeError("Capture has not finished")
        self._executor.shutdown(wait=False)

    def cancel_publication(self):
        """An explicit Quit keeps the stopped WAVs without waiting for encode."""
        self._publication_cancel.set()
        if self._publication_future is not None:
            self._publication_future.cancel()
