# SPDX-License-Identifier: GPL-3.0-or-later
"""Separate native PCM tracks on one GStreamer clock, checkpointed off GTK."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from gi.repository import GLib, GObject, Gst

from .capture_sources import change_time, selected_pcm
from .recorder import request_eos
from .recording_details import space_left
from .recording_session import RecordingSession

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
        self._timeout = 0
        self._disk_timer = 0
        self._disk_future = None
        self._changing_sources = False
        self._track_ids = {"microphone": "microphone", "system": "system"}
        self._boundaries = {"microphone": 0, "system": 0}
        self._branches = {}

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
        self.levels = [0.0, 0.0]
        self._track_ids = {"microphone": "microphone", "system": "system"}
        self._boundaries = {"microphone": 0, "system": 0}
        self._branches = {}
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
        branch = Gst.parse_bin_from_description(
            source + " ! queue ! audioconvert ! audioresample ! "
            "audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
            f"level name=level-{kind} interval=100000000 post-messages=true ! "
            f"appsink name={kind} emit-signals=true sync=false max-buffers=8 drop=false",
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

        desired = {kind for kind, enabled in (("microphone", me), ("system", others)) if enabled}
        removed, added = self._track_ids.keys() - desired, desired - self._track_ids.keys()
        if not removed and not added:
            return source_state(self.session.manifest.get("events"))
        branches = {kind: self._branch(kind) for kind in added}
        clock = self.pipeline.get_clock()
        elapsed = (clock.get_time() - self.pipeline.get_base_time()) / Gst.MSECOND
        with self._lock:
            at = change_time(self.session, elapsed)
            events = [dict(atMs=at, type="selection", me=me, others=others, reason="user")]
            if "system" in removed:
                events.append(dict(atMs=at, type="othersSource", source="none", reason="user"))
            elif "system" in added:
                events.append(dict(atMs=at, type="othersSource", source="system", reason="user"))
            identifiers = dict(self._track_ids)
            with self.session.change(events):
                for kind in removed:
                    self.session.end_track(identifiers.pop(kind))
                for kind in added:
                    identifier = kind + "-" + str(uuid4())
                    self.session.add_track(
                        identifier, text("common.me" if kind == "microphone" else "common.theOthers"), kind, RATE, 1
                    )
                    self.session.manifest["tracks"][-1]["startOffsetMs"] = at
                    identifiers[kind] = identifier
            self._track_ids = identifiers
            for kind in added:
                self._boundaries[kind] = at
        for kind in removed:
            branch = self._branches.pop(kind)
            branch.set_state(Gst.State.NULL)
            self.pipeline.remove(branch)
            self.levels[0 if kind == "microphone" else 1] = 0
        for kind, branch in branches.items():
            self.pipeline.add(branch)
            self._branches[kind] = branch
            if not branch.sync_state_with_parent():
                raise OSError("Could not start native audio capture")
        return source_state(self.session.manifest.get("events"))

    def _sources_changed(self, future):
        self._changing_sources = False
        try:
            future.result()
        except ValueError:
            pass  # The caller displays the catalog change-failed line.
        except Exception as error:
            self._begin_finish(str(error))
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
            with self._lock:
                stored = self._track_ids.get(identifier)
                if stored is not None:
                    data, offset = selected_pcm(data, buffer.pts / Gst.MSECOND, RATE, 1, self._boundaries[identifier])
                    if data:
                        self.session.append(stored, data, offset)
        except Exception as error:
            GLib.idle_add(self._begin_finish, str(error))
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def _message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            self._begin_finish(message.parse_error()[0].message)
        elif message.type == Gst.MessageType.EOS:
            self._begin_finish()
        elif message.type == Gst.MessageType.ELEMENT:
            value = message.get_structure()
            if value is not None and value.get_name() == "level":
                identifier = message.src.get_name()
                index = 0 if identifier == "level-microphone" else 1
                db = value.get_value("rms")[0]
                kind = "microphone" if index == 0 else "system"
                self.levels[index] = max(0.0, min(1.0, (db + 60) / 60)) if kind in self._track_ids else 0
                self.emit("changed")

    def stop(self):
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
            with self._lock:
                session.finish(problem)

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
