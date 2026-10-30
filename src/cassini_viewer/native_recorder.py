# SPDX-License-Identifier: GPL-3.0-or-later
"""Separate native PCM tracks on one GStreamer clock, checkpointed off GTK."""

import contextlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from gi.repository import GLib, GObject, Gst

from .capture_sources import change_time, selected_pcm
from .native_timing import NS, NativeTimeline
from .recorder import request_eos
from .recording_details import ACMeter, space_left
from .recording_session import RecordingSession
from .spectro_source import SpectroSource

RATE = 48000


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
        self._ac_meters = {}
        self.spectrogram = SpectroSource()

    @property
    def activity(self):
        if self.pipeline is None or self.session is None:
            return {}
        return {self.session.manifest["id"]: "saving" if self.stopping else "recording"}

    def start(self, parent=None, *, test=False):
        if self.pipeline is not None or self.starting or self.stopping or self._changing_sources or self._finishing:
            raise RuntimeError("Already recording")
        self.starting = True
        self._stop_requested = False
        self.session = None
        self.stopping = self._finishing = False
        self._problem = self.line = None
        self.end_reason = None
        self.levels = [0.0, 0.0]
        self._track_ids = {"microphone": "microphone", "system": "system"}
        self._boundaries = {"microphone": 0, "system": 0}
        self._branches = {}
        self._pending_sources = {}
        self._retired_branches = set()
        self._timelines = {}
        self._ac_meters = {}
        self.spectrogram = SpectroSource()
        future = self._executor.submit(self._prepare, Path(parent) if parent is not None else None)
        future.add_done_callback(lambda f: GLib.idle_add(self._prepared, f, test))

    @staticmethod
    def _prepare(parent):
        from .strings import text

        session = RecordingSession(parent, source="gnome", title="")
        try:
            with session.change(
                [
                    dict(atMs=0, type="othersSource", source="system", reason="missing"),
                    dict(atMs=0, type="selection", me=True, others=True, reason="start"),
                ]
            ):
                session.add_track(
                    "microphone", text("common.me"), "microphone", RATE, 1, filename="track-microphone.wav"
                )
                session.add_track("system", text("common.theOthers"), "system", RATE, 1, filename="track-system.wav")
                session.manifest["tracks"][-1]["startOffsetMs"] = 0
            return session, space_left(session.directory)
        except Exception as error:
            session.finish(str(error))
            raise

    def _prepared(self, future, test):
        self.starting = False
        try:
            self.session, (self.minutes_left, reserve) = future.result()
            for track in self.session.manifest["tracks"]:
                self.spectrogram.declare(track["id"], kind=track["kind"])
            if self._stop_requested:
                self._begin_finish()
                return False
            self._test_sources = test
            self.pipeline = Gst.Pipeline.new(None)
            # Device clocks reset independently when Pulse streams start. Use
            # one monotonic clock for both sources and their running-time/base
            # mapping; keep the audio source's normal clock-slaving behaviour.
            self.pipeline.use_clock(Gst.SystemClock.obtain())
            for identifier in ("microphone", "system"):
                branch = self._branch(identifier)
                self.pipeline.add(branch)
                self._branches[identifier] = branch
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            self._bus_handler = bus.connect("message", self._message)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Could not start audio capture. Check your audio devices and GStreamer plugins.")
            self._start_us = GLib.get_monotonic_time()
            if self.minutes_left < 10:
                self.line = "errors.diskLow.line"
            self._disk_timer = GLib.timeout_add_seconds(1, self._check_disk)
            self.emit("started", str(self.session.directory))
            self.emit("changed")
            if reserve:
                self.line = "errors.diskStopped.line"
                self.stop()
            elif self._stop_requested:
                self.stop()
        except Exception as error:
            if self.session is not None:
                self._begin_finish(str(error))
            else:
                self.emit("error", str(error))
                self.emit("finished", "")
        return False

    def _branch(self, kind):
        device = "@DEFAULT_SOURCE@" if kind == "microphone" else "@DEFAULT_MONITOR@"
        source = (
            f"audiotestsrc is-live=true freq={440 if kind == 'microphone' else 880}"
            if self._test_sources
            else f'pulsesrc device={device} client-name="Cassini Recording" provide-clock=false'
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

    def select(self, me, others):
        if type(me) is not bool or type(others) is not bool or not (me or others):
            raise ValueError("Select at least one recording source")
        if self.pipeline is None or self.stopping or self._finishing or self._changing_sources:
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
        if not removed and not added:
            return source_state(self.session.manifest.get("events"))
        pipeline = self.pipeline
        prepared = {}
        committed = False
        try:
            for kind in sorted(added):
                branch = self._branch(kind)
                attempt = dict(branch=branch, ready=threading.Event(), error=None)
                prepared[kind] = attempt
                with self._lock:
                    self._pending_sources[kind] = attempt
                pipeline.add(branch)
                if not branch.sync_state_with_parent():
                    raise OSError("Could not start native audio capture")
                if not attempt["ready"].wait(3):
                    raise OSError("Native audio capture did not produce a sample")
                if attempt["error"]:
                    raise OSError(attempt["error"])
            clock = pipeline.get_clock()
            elapsed = (clock.get_time() - pipeline.get_base_time()) / Gst.MSECOND
            with self._lock:
                if self.pipeline is not pipeline or self.stopping or self._finishing or self._stop_requested:
                    raise ValueError("No source change is available")
                for attempt in prepared.values():
                    if attempt["error"]:
                        raise OSError(attempt["error"])
                at = change_time(self.session, elapsed)
                events = [dict(atMs=at, type="selection", me=me, others=others, reason="user")]
                if "system" in removed:
                    events.append(dict(atMs=at, type="othersSource", source="none", reason="user"))
                elif "system" in added:
                    events.append(dict(atMs=at, type="othersSource", source="system", reason="user"))
                identifiers = dict(self._track_ids)
                retired_ids = [identifiers[kind] for kind in removed]
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
            for kind in added:
                self.spectrogram.declare(self._track_ids[kind], kind=kind, now_ms=at)
            for kind in removed:
                self.levels[0 if kind == "microphone" else 1] = 0
            for branch in retired:
                branch.set_state(Gst.State.NULL)
                pipeline.remove(branch)
            return source_state(self.session.manifest.get("events"))
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

    def _sources_changed(self, future):
        self._changing_sources = False
        # Keep the old source; the caller displays the catalog failure.
        with contextlib.suppress(Exception):
            future.result()
        self.emit("changed")
        if self._stop_requested and not self.stopping and not self._finishing:
            self.stop()
        return False

    def _sample(self, sink, identifier):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        try:
            if buffer.pts == Gst.CLOCK_TIME_NONE:
                raise ValueError("Audio source has no clock timestamp")
            data = buffer.extract_dup(0, buffer.get_size())
            picture = None
            with self._lock:
                attempt = self._pending_sources.get(identifier)
                if attempt is not None:
                    attempt["ready"].set()
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
                    data, offset = selected_pcm(
                        data, buffer.pts / Gst.MSECOND, rate, channels, self._boundaries[identifier]
                    )
                    if data:
                        frames = len(data) // (channels * 2)
                        host_ns = buffer.pts + (original_frames - frames) * NS // rate
                        timeline = self._timelines.setdefault(stored, NativeTimeline(rate))
                        offset, gap = timeline.map(host_ns, frames, offset)
                        self.session.append_native(stored, data, offset, gap_frames=gap)
                        source = self.spectrogram
                        if source.active:
                            track = next(t for t in self.session.manifest["tracks"] if t["id"] == stored)
                            picture = (source, stored, data, offset, rate, channels, track["startOffsetMs"], gap)
            # No conversion or FFT while the WAV writer/selection lock is held.
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
        except Exception as error:
            with self._lock:
                attempt = self._pending_sources.get(identifier)
                if attempt is not None:
                    attempt["error"] = str(error)
                    attempt["ready"].set()
                else:
                    GLib.idle_add(self._begin_finish, str(error))
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def _message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            problem = message.parse_error()[0].message
            with self._lock:
                for attempt in self._pending_sources.values():
                    branch = attempt["branch"]
                    if message.src == branch or message.src.has_as_ancestor(branch):
                        attempt["error"] = problem
                        attempt["ready"].set()
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
            return
        if self._changing_sources:
            self._stop_requested = True
            return
        if self.pipeline is None or self.stopping:
            return
        self.stopping = True
        self.emit("changed")
        self._timeout = GLib.timeout_add_seconds(10, self._timed_out)
        request_eos(self.pipeline, self._eos_rejected)

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
        self._problem = problem
        self.emit("changed")
        for timer in (self._timeout, self._disk_timer):
            if timer:
                GLib.source_remove(timer)
        self._timeout = self._disk_timer = 0
        pipeline, session = self.pipeline, self.session

        def finish():
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            try:
                with self._lock:
                    session.finish(problem)
            finally:
                self.spectrogram.finish()
            reason = "error" if problem else self.end_reason
            if reason:
                from .capture_facts import record_end

                try:
                    record_end(session.directory, reason, manifest=session.manifest)
                except (OSError, ValueError):
                    logging.getLogger(__name__).warning("Could not save recording end details", exc_info=True)

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
        self.emit("finished", str(self.session.directory))
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
