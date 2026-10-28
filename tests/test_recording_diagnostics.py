# SPDX-License-Identifier: GPL-3.0-or-later
"""Failure evidence survives pipeline disposal and preserves the original append error."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from cassini_viewer.recording_session import ProtocolError, RecordingSession  # noqa: E402

Gst.init(None)
Evidence = runpy.run_path(str(Path(__file__).parent / "recording/gst_evidence.py"))["GstEvidence"]


def test_bus_debug_source_and_graph_are_saved_before_pipeline_disposal(tmp_path):
    pipeline = Gst.Pipeline.new("private-test")
    source = Gst.ElementFactory.make("fakesrc", "failed-source")
    pipeline.add(source)
    trace = Evidence(tmp_path)

    class Recorder:
        def __init__(self):
            self.pipeline = pipeline

        def _message(self, bus, message):
            self.pipeline = None

    trace.hook_messages(Recorder)
    try:
        error = GLib.Error.new_literal(Gst.StreamError.quark(), "Internal data stream error.", Gst.StreamError.FAILED)
        message = Gst.Message.new_error(source, error, "streaming stopped, reason not-negotiated (-4)")
        recorder = Recorder()
        recorder._message(None, message)
        assert recorder.pipeline is None
        saved = json.loads((tmp_path / "gst-evidence.json").read_text())
        assert saved["errors"][0]["debug"] == "streaming stopped, reason not-negotiated (-4)"
        assert "failed-source" in saved["errors"][0]["source"]
        assert "failed-source" in saved["errors"][0]["pipeline"]["sources"][0]["name"]
        assert "failed-source" in (tmp_path / "gst-before-failure.dot").read_text()
        with pytest.raises(AssertionError, match="not-negotiated"):
            trace.healthy("waiting for PCM")
    finally:
        trace.close()


def test_append_failure_preserves_precise_error_offset_and_track_before_generic_bus_error(tmp_path):
    session = RecordingSession(tmp_path / "library")
    session.add_track("mic", "Me", "microphone", 48000, 1)
    trace = Evidence(tmp_path)
    trace.hook_appends(RecordingSession)
    try:
        with pytest.raises(ProtocolError):
            session.append("mic", b"\x00\x00", float("inf"))
        saved = json.loads((tmp_path / "gst-evidence.json").read_text())
        entry = saved["errors"][0]
        assert entry["type"] == "append-failure" and entry["track"] == "mic"
        assert entry["offset"] == "inf" and entry["frames"] == 0
        assert entry["exception"] == "ProtocolError" and entry["traceback"]
    finally:
        trace.close()
        session.finish()


def test_legacy_failure_is_checkpointed_before_a_blocking_cleanup(tmp_path):
    trace = Evidence(tmp_path)
    called = []

    class Recorder:
        def _fail(self, message):
            assert (tmp_path / "gst-evidence.json").exists()
            called.append(message)

    trace.hook_failure(Recorder)
    recorder = SimpleNamespace(pipeline=None)
    try:
        Recorder._fail(recorder, "Timed out while saving")
        assert called == ["Timed out while saving"]
        assert json.loads((tmp_path / "gst-evidence.json").read_text())["errors"][0]["message"] == called[0]
    finally:
        trace.close()
