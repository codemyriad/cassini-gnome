# SPDX-License-Identifier: GPL-3.0-or-later
"""Input-clock conformance; no microphone, sound server or desktop connection."""

import json
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from cassini_talk import recording_session
from cassini_talk.native_timing import NativeTimeline
from cassini_talk.recording_session import ProtocolError, RecordingSession, WAVWriter

FIXTURE = Path(__file__).parents[1] / "browser-protocol/conformance/native-session-gap"


def recorder_class():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("GStreamer typelib is unavailable")
    from gi.repository import Gst

    from cassini_talk.native_recorder import NativeRecorder

    Gst.init(None)
    return NativeRecorder, Gst


def feed(recorder, identifier, data, pts):
    buffer = SimpleNamespace(pts=pts, get_size=lambda: len(data), extract_dup=lambda _start, _length: data)
    sample = SimpleNamespace(get_buffer=lambda: buffer)
    return recorder._sample(SimpleNamespace(emit=lambda _action: sample), identifier)


@pytest.mark.parametrize("offset", [-2060, 4000])
def test_native_meter_ignores_dc_and_original_pcm_is_saved(tmp_path, offset):
    NativeRecorder, Gst = recorder_class()
    session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    recorder = NativeRecorder()
    recorder.session = session
    pcm = offset.to_bytes(2, "little", signed=True) * 4800
    try:
        assert feed(recorder, "microphone", pcm, 0) == Gst.FlowReturn.OK
        assert recorder.levels[0] == 0
        message = SimpleNamespace(
            type=Gst.MessageType.ELEMENT,
            src=SimpleNamespace(get_name=lambda: "level-microphone"),
            get_structure=lambda: SimpleNamespace(
                get_name=lambda: "level", get_value=lambda _key: pytest.fail("Raw RMS hides silent DC")
            ),
        )
        recorder._message(None, message)
        assert recorder.levels[0] == 0
        session.finish()
        with wave.open(str(session.directory / session.manifest["tracks"][0]["file"])) as audio:
            assert audio.readframes(audio.getnframes()) == pcm
    finally:
        session.finish()
        recorder.close()


def test_browser_native_tap_measures_ac_without_replacing_pcm():
    _NativeRecorder, Gst = recorder_class()
    from cassini_talk.native_tap import NativeTap

    received = []
    tap = NativeTap("microphone", 0, lambda data, *_args, **_kw: received.append(data), pytest.fail)
    tap.pipeline = SimpleNamespace(get_base_time=lambda: 0)
    tap.anchor_wall = tap.anchor_clock = 0
    pcm = (-2060).to_bytes(2, "little", signed=True) * 4800
    buffer = SimpleNamespace(pts=0, get_size=lambda: len(pcm), extract_dup=lambda *_: pcm)
    sample = SimpleNamespace(get_buffer=lambda: buffer)
    assert tap._sample(SimpleNamespace(emit=lambda _: sample)) == Gst.FlowReturn.OK
    assert tap._ac_meter.level == 0 and received == [pcm]


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
        assert {
            key: value for key, value in session.manifest.items() if key not in ("events", "startedAtUtcOffsetMinutes")
        } == expected
        assert session.manifest["events"] == [dict(type="end", atMs=3000, reason="user")]
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
    # This stress test models ten hours of clock stamps with a counting writer,
    # not 1.7 billion samples of inference/metering. Real appsink AC windows and
    # byte-identical saved PCM are exercised separately above.
    recorder._ac_meters["microphone"] = SimpleNamespace(feed=lambda _data: False)
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


def test_spectrogram_observes_only_admitted_pcm_after_the_writer_lock(tmp_path, monkeypatch):
    from test_spectro_analysis import tone

    NativeRecorder, Gst = recorder_class()
    session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    recorder = NativeRecorder()
    recorder.session = session
    source = recorder.spectrogram
    sub = source.subscribe()
    observed, observe = [], source.observe

    def outside(*args, **kwargs):
        assert recorder._lock.acquire(False), "FFT ran under the WAV/selection lock"
        recorder._lock.release()
        observed.append((args, kwargs))
        return observe(*args, **kwargs)

    monkeypatch.setattr(source, "observe", outside)
    pcm = tone(4800, amplitude=500)
    try:
        assert feed(recorder, "microphone", pcm, 0) == Gst.FlowReturn.OK
        assert len(observed) == 1 and observed[0][0][1] == pcm
        assert source.drain(sub, 100)["me"]
        source.unsubscribe(sub)
        before = source.analysed
        assert feed(recorder, "microphone", pcm, 100_000_000) == Gst.FlowReturn.OK
        assert source.analysed == before and len(observed) == 1
        recorder._pending_sources["microphone"] = dict(ready=threading.Event())
        assert feed(recorder, "microphone", pcm, 200_000_000) == Gst.FlowReturn.OK
        assert len(observed) == 1, "Readiness PCM reached the picture"
        recorder._pending_sources.clear()
        session.finish()
        with wave.open(str(session.directory / session.manifest["tracks"][0]["file"])) as audio:
            assert audio.readframes(audio.getnframes()) == pcm + pcm
    finally:
        session.finish()
        recorder.close()


def test_spectrogram_failure_never_interrupts_saved_pcm(tmp_path, monkeypatch):
    NativeRecorder, Gst = recorder_class()
    session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    recorder = NativeRecorder()
    recorder.session = session
    recorder.spectrogram.subscribe()

    def failure(*args, **kwargs):
        raise OSError("synthetic analysis failure")

    monkeypatch.setattr(recorder.spectrogram, "feed", failure)
    pcm = (-2060).to_bytes(2, "little", signed=True) * 4800
    try:
        for second in range(2):
            assert feed(recorder, "microphone", pcm, second * 100_000_000) == Gst.FlowReturn.OK
        assert recorder.spectrogram.problem == "synthetic analysis failure"
        session.finish()
        with wave.open(str(session.directory / session.manifest["tracks"][0]["file"])) as audio:
            assert audio.readframes(audio.getnframes()) == pcm + pcm
    finally:
        session.finish()
        recorder.close()


def test_late_spectrogram_subscription_keeps_the_original_sample_grid(tmp_path):
    from test_spectro_analysis import tone

    NativeRecorder, Gst = recorder_class()
    session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    recorder = NativeRecorder()
    recorder.session = session
    try:
        assert feed(recorder, "microphone", tone(1537), 0) == Gst.FlowReturn.OK
        assert not recorder.spectrogram.tracks and recorder._spectro_starts["microphone"] == 0
        subscription = recorder.spectrogram.subscribe()
        assert feed(recorder, "microphone", tone(4800), 1537 * 1_000_000_000 // 48000) == Gst.FlowReturn.OK
        rows = recorder.spectrogram.drain(subscription, 200)["me"]
        assert rows and rows[0][0] == 2
    finally:
        session.finish()
        recorder.close()
