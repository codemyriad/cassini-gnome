# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded GLib waits and PCM observations, only for the isolated test harness."""

import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


def wait_until(check, description, *, timeout=12, diagnostics=lambda: {}):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # A busy producer must not starve the deadline or condition check.
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        value = check()
        if value:
            return value
        time.sleep(0.01)  # Poll interval, never a startup/capture-duration assumption.
    raise AssertionError(f"Timed out after {timeout}s: {description}; diagnostics={diagnostics()!r}")


class PCMProgress:
    """Count real complete S16LE frames observed on a streaming pad, thread-safely."""

    def __init__(self, pad, *, observe_audio=False):
        self.pad = pad
        self.observe_audio = observe_audio
        self._lock = threading.Lock()
        self._value = dict(frames=0, audibleFrames=0, sampleRate=0, channels=0)
        self._probe = pad.add_probe(Gst.PadProbeType.BUFFER, self._buffer)

    def _buffer(self, pad, info):
        buffer = info.get_buffer()
        caps = pad.get_current_caps()
        if buffer is None or caps is None:
            return Gst.PadProbeReturn.OK
        structure = caps.get_structure(0)
        if structure.get_string("format") != "S16LE":
            return Gst.PadProbeReturn.OK
        rate, channels = structure.get_value("rate"), structure.get_value("channels")
        frames = buffer.get_size() // (2 * channels)
        audible = False
        if self.observe_audio:
            mapped, mapping = buffer.map(Gst.MapFlags.READ)
            if mapped:
                try:
                    audible = any(mapping.data)
                finally:
                    buffer.unmap(mapping)
        with self._lock:
            self._value["frames"] += frames
            self._value["sampleRate"], self._value["channels"] = rate, channels
            self._value["audibleFrames"] = self._value["audibleFrames"] + frames if audible else 0
        return Gst.PadProbeReturn.OK

    def snapshot(self):
        with self._lock:
            return dict(self._value)

    def reached(self, seconds, *, audible=False):
        value = self.snapshot()
        return (
            value["sampleRate"] > 0 and value["audibleFrames" if audible else "frames"] >= value["sampleRate"] * seconds
        )

    def close(self):
        if self._probe:
            self.pad.remove_probe(self._probe)
            self._probe = 0
