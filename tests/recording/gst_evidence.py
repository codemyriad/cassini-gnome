# SPDX-License-Identifier: GPL-3.0-or-later
"""Passive failure evidence before recorders dispose a failed pipeline; test-only."""

import json
import math
import time
import traceback
from collections import deque

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


def pipeline_state(pipeline):
    if pipeline is None:
        return None
    state = pipeline.get_state(0)
    clock = pipeline.get_clock()
    sources = []
    iterator = pipeline.iterate_sources()
    while True:
        result, source = iterator.next()
        if result == Gst.IteratorResult.RESYNC:
            iterator.resync()
        elif result == Gst.IteratorResult.OK:
            pad = source.get_static_pad("src")
            caps = pad.get_current_caps() if pad else None
            values = dict(name=source.get_path_string(), caps=caps.to_string() if caps else None)
            for key in ("device", "current-device", "provide-clock"):
                if source.find_property(key):
                    values[key] = source.get_property(key)
            sources.append(values)
        else:
            break
    return dict(state=[value.value_nick for value in state], clock=clock.get_name() if clock else None, sources=sources)


class GstEvidence:
    def __init__(self, directory):
        self.directory = directory
        self.events = deque(maxlen=64)
        self.errors = []
        self.restores = []
        self.watches = []
        self.phase = "setup"
        self.last_pipeline = None

    def snapshot(self):
        return dict(phase=self.phase, errors=list(self.errors), events=list(self.events), pipeline=self.last_pipeline)

    def checkpoint(self, phase, pipeline=None):
        self.phase = phase
        self.last_pipeline = pipeline_state(pipeline)
        if pipeline is not None:
            (self.directory / ("gst-" + phase + ".dot")).write_text(
                Gst.debug_bin_to_dot_data(pipeline, Gst.DebugGraphDetails.ALL)
            )
        self.persist()

    def persist(self):
        (self.directory / "gst-evidence.json").write_text(json.dumps(self.snapshot(), indent=2, allow_nan=False) + "\n")

    def failed(self, record, pipeline=None):
        if pipeline is not None:
            self.last_pipeline = pipeline_state(pipeline)
        record = dict(record, pipeline=self.last_pipeline)
        self.errors.append(record)
        if pipeline is not None:
            (self.directory / "gst-before-failure.dot").write_text(
                Gst.debug_bin_to_dot_data(pipeline, Gst.DebugGraphDetails.ALL)
            )
        self.persist()

    def healthy(self, description):
        assert not self.errors, f"Capture failed during {description}: {self.snapshot()!r}"

    def message(self, label, pipeline, message):
        if message.type in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
            error, debug = message.parse_error() if message.type == Gst.MessageType.ERROR else message.parse_warning()
            record = dict(
                at=time.monotonic(),
                type=message.type.value_nicks[-1],
                label=label,
                source=message.src.get_path_string() if message.src else None,
                message=error.message,
                domain=error.domain,
                code=error.code,
                debug=debug,
            )
            self.events.append(record)
            if message.type == Gst.MessageType.ERROR:
                self.failed(record, pipeline)
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == pipeline:
            self.events.append(
                dict(label=label, type="state", states=[s.value_nick for s in message.parse_state_changed()])
            )

    def hook_messages(self, cls):
        original = cls._message
        trace = self

        def wrapped(recorder, bus, message):
            trace.message(cls.__name__, recorder.pipeline, message)
            return original(recorder, bus, message)

        cls._message = wrapped
        self.restores.append(lambda: setattr(cls, "_message", original))

    def hook_failure(self, cls):
        original = cls._fail
        trace = self

        def wrapped(recorder, message):
            trace.failed(dict(type="recorder-failure", message=message), recorder.pipeline)
            return original(recorder, message)

        cls._fail = wrapped
        self.restores.append(lambda: setattr(cls, "_fail", original))

    def hook_appends(self, cls):
        original = cls.append
        trace = self

        def wrapped(session, identifier, data, offset):
            try:
                return original(session, identifier, data, offset)
            except Exception as error:
                writer = session.writers.get(identifier) if isinstance(identifier, str) else None
                trace.failed(
                    dict(
                        type="append-failure",
                        session=session.manifest["id"],
                        track=identifier,
                        offset=offset if isinstance(offset, (int, float)) and math.isfinite(offset) else repr(offset),
                        bytes=len(data),
                        frames=writer.frames if writer else None,
                        message=str(error),
                        exception=type(error).__name__,
                        traceback=traceback.format_exc(),
                    )
                )
                raise

        cls.append = wrapped
        self.restores.append(lambda: setattr(cls, "append", original))

    def watch(self, pipeline, label):
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        handler = bus.connect("message", lambda _bus, message: self.message(label, pipeline, message))
        self.watches.append((bus, handler))

    def close(self):
        for bus, handler in self.watches:
            bus.disconnect(handler)
            bus.remove_signal_watch()
        self.watches.clear()
        for restore in reversed(self.restores):
            restore()
        self.restores.clear()
