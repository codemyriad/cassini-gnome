# SPDX-License-Identifier: GPL-3.0-or-later
"""Input-clock conformance; no microphone, sound server or desktop connection."""

import json
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from cassini_viewer import recording_session
from cassini_viewer.native_timing import NativeTimeline
from cassini_viewer.recording_session import ProtocolError, RecordingSession, WAVWriter

FIXTURE = Path(__file__).parents[1] / "browser-protocol/conformance/native-session-gap"


def recorder_class():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("GStreamer typelib is unavailable")
    from gi.repository import Gst

    from cassini_viewer.native_recorder import NativeRecorder

    Gst.init(None)
    return NativeRecorder, Gst


def feed(recorder, identifier, data, pts):
    buffer = SimpleNamespace(pts=pts, get_size=lambda: len(data), extract_dup=lambda _start, _length: data)
    sample = SimpleNamespace(get_buffer=lambda: buffer)
    return recorder._sample(SimpleNamespace(emit=lambda _action: sample), identifier)


def test_pinned_gnome_gap_fixture_through_native_input_callbacks(tmp_path, monkeypatch):
    NativeRecorder, Gst = recorder_class()
    expected = json.loads((FIXTURE / "session-gnome.json").read_text())
    monkeypatch.setattr(recording_session, "uuid4", lambda: UUID(expected["id"]))
    session = RecordingSession(tmp_path, title=expected["title"], source="gnome")
    session.manifest["createdAt"] = expected["createdAt"]
    recorder = NativeRecorder()
    recorder.session = session
    try:
        for track in expected["tracks"]:
            session.add_track(
                track["id"],
                track["label"],
                track["kind"],
                track["sampleRate"],
                track["channels"],
                label_source="device",
                filename=track["file"],
            )
            with wave.open(str(FIXTURE / track["file"])) as audio:
                first = audio.readframes(8000)
                assert feed(recorder, track["id"], first, 0) == Gst.FlowReturn.OK
                if track["kind"] == "system":
                    assert audio.readframes(8000) == bytes(8000 * 4)
                    assert feed(recorder, track["id"], audio.readframes(8000), 2_000_000_000) == Gst.FlowReturn.OK
                else:
                    for second in (1, 2):
                        assert (
                            feed(recorder, track["id"], audio.readframes(8000), second * 1_000_000_000)
                            == Gst.FlowReturn.OK
                        )
        session.finish()
        assert session.manifest == expected
        for track in expected["tracks"]:
            assert (session.directory / track["file"]).read_bytes() == (FIXTURE / track["file"]).read_bytes()
    finally:
        session.finish()
        recorder.close()


class CountingWriter:
    def __init__(self, _path, rate, channels):
        self.rate, self.channels, self.frames, self.padding = rate, channels, 0, 0

    def append(self, data):
        self.frames += len(data) // (2 * self.channels)

    def silence(self, frames):
        self.padding += frames
        self.frames += frames

    def sync(self):
        pass

    def close(self):
        pass


def test_ten_hours_smooth_device_drift_does_not_become_missing_audio(tmp_path):
    NativeRecorder, Gst = recorder_class()
    session = RecordingSession(tmp_path, source="gnome", clock=lambda: 0, writer_factory=CountingWriter)
    session.add_track("microphone", "Microphone", "microphone", 48000, 1)
    recorder = NativeRecorder()
    recorder.session = session
    data = bytes(48000 * 2)
    try:
        for second in range(36000):
            assert feed(recorder, "microphone", data, 700_000_000 + second * 1_000_050_000) == Gst.FlowReturn.OK
        assert session.writers["microphone"].padding == 0
        session.finish()
        assert session.manifest["tracks"][0]["frames"] == 36000 * 48000
        assert session.manifest["tracks"][0]["startOffsetMs"] == 700
        assert "gapFrames" not in session.manifest["tracks"][0]
        assert session.manifest["state"] == "completed"
    finally:
        session.finish()
        recorder.close()


@pytest.mark.parametrize("extra_ns,gap", [(999_937_499, 0), (999_937_500, 8000), (1_000_000_000, 8000)])
def test_positive_half_frame_rounding_before_one_second_threshold(extra_ns, gap):
    clock = NativeTimeline(8000)
    assert clock.map(0, 8000, 0) == (0, 0)
    assert clock.map(1_000_000_000 + extra_ns, 8000, 1000 + extra_ns / 1_000_000) == (1000 + gap / 8, gap)


def test_negative_or_small_skew_is_diagnostic_and_never_reorders_samples():
    clock = NativeTimeline(8000)
    assert clock.map(3_000_000_000, 8000, 3000) == (3000, 0)
    assert clock.map(2_000_000_000, 8000, 2000) == (4000, 0)
    assert clock.map(3_500_000_000, 8000, 3500) == (5000, 0)
    assert clock.diagnostic_buffers == 2


def test_long_native_gap_is_not_limited_by_browser_wire_padding(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("system", "System", "system", 8000, 1)
    try:
        session.append_native("system", b"\1\0" * 8, 0)
        session.append_native("system", b"\2\0" * 8, 61001, gap_frames=61 * 8000)
        session.finish()
        assert session.manifest["tracks"][0]["gapFrames"] == 61 * 8000
        assert session.manifest["tracks"][0]["frames"] == 61 * 8000 + 16
        assert session.manifest["problemKind"] == "gap"
    finally:
        session.finish()


def test_partial_silence_on_wav_limit_counts_only_written_frames_and_io_wins(tmp_path):
    session = RecordingSession(
        tmp_path, source="gnome", writer_factory=lambda p, r, c: WAVWriter(p, r, c, max_bytes=16400)
    )
    session.add_track("system", "System", "system", 8000, 1)
    try:
        session.append_native("system", b"\1\0" * 8, 0)
        with pytest.raises(ProtocolError, match="WAV size limit"):
            session.append_native("system", b"\2\0" * 8, 2001, gap_frames=16000)
        session.finish()
        (track,) = session.manifest["tracks"]
        assert track["gapFrames"] == 8192 and track["frames"] == 8200
        assert session.manifest["problemKind"] == "io"
        with wave.open(str(session.directory / track["file"])) as audio:
            assert audio.getnframes() == track["frames"]
    finally:
        session.finish()


def test_selection_prefix_before_first_buffer_is_aligned_but_not_a_loss(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("system", "System", "system", 8000, 1)
    session.manifest["tracks"][0]["startOffsetMs"] = 0
    try:
        session.append_native("system", b"\1\0" * 8000, 10)
        session.finish()
        (track,) = session.manifest["tracks"]
        assert track["frames"] == 8080 and track["endOffsetMs"] == 1010
        assert "gapFrames" not in track and session.manifest["state"] == "completed"
        with wave.open(str(session.directory / track["file"])) as audio:
            assert audio.readframes(80) == bytes(160)
            assert audio.readframes(8000) == b"\1\0" * 8000
    finally:
        session.finish()
