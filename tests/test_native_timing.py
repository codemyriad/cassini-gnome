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
from cassini_talk.native_timing import NS, NativeTimeline
from cassini_talk.recording_session import MAX_CHUNK, ProtocolError, RecordingSession, WAVWriter, chunk_plan

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


class Sink:
    """One appsink: a branch keeps its identity across buffers."""

    def __init__(self, source=None):
        self.sample, self.source = None, source

    def emit(self, _action):
        return self.sample

    def get_parent(self):
        return SimpleNamespace(get_by_name=lambda name: self.source if name == "capture-source" else None)


def pulse_source(latency_us, rate):
    """The negotiated pulsesrc facts GstAudioBaseSrc exposes once its ring buffer is acquired."""
    _NativeRecorder, Gst = recorder_class()
    caps = Gst.Caps.from_string(f"audio/x-raw,format=S16LE,rate={rate},channels=1")
    pad = SimpleNamespace(get_current_caps=lambda: caps)
    return SimpleNamespace(
        get_static_pad=lambda _name: pad, get_property={"actual-latency-time": latency_us}.__getitem__
    )


def feed(recorder, identifier, data, pts, sink=None):
    buffer = SimpleNamespace(pts=pts, get_size=lambda: len(data), extract_dup=lambda _start, _length: data)
    sink = sink or Sink()
    sink.sample = SimpleNamespace(get_buffer=lambda: buffer)
    return recorder._sample(sink, identifier)


def scheduled_problems(monkeypatch, recorder):
    """Run no GTK callback; return the problems a sample handed to _begin_finish."""
    from cassini_talk import native_recorder

    scheduled = []
    monkeypatch.setattr(native_recorder.GLib, "idle_add", lambda function, *args: scheduled.append((function, args)))
    return lambda: [args[0] for function, args in scheduled if function == recorder._begin_finish]


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


# GstAudioBaseSrc's first-buffer skew resync wrapped by 2^32 segments of 10 ms.
WRAPPED_NS = 2**32 * 480 * NS // 48000


class CountingClock:
    def __init__(self, base, now):
        self.base, self.now, self.lookups = base, now, 0

    def pipeline(self):
        def get_clock():
            self.lookups += 1
            return SimpleNamespace(get_time=lambda: self.base + self.now)

        return SimpleNamespace(get_base_time=lambda: self.base, get_clock=get_clock)


def start_with_a_wrapped_system_stamp(tmp_path, monkeypatch, source, system_pts, *, fail_second=False):
    """CI desktop-mic-149/159: the monitor's first stamp lay ~497 days ahead; Record ended at 80 ms."""
    NativeRecorder, Gst = recorder_class()
    # A Library session saves native clock anchors (S-12).
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    session = RecordingSession(None, source="gnome")
    session._native_clock_origin_ns = NS
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    session.add_track("system", "The others", "system", 48000, 1)
    session.manifest["tracks"][-1]["startOffsetMs"] = 0
    recorder = NativeRecorder()
    recorder.session = session
    problems = scheduled_problems(monkeypatch, recorder)
    clock = CountingClock(5 * NS, 80_000_000)
    recorder.pipeline = clock.pipeline()
    mic, system = Sink(pulse_source(10000, 48000)), Sink(source)
    pcm = (1000).to_bytes(2, "little", signed=True) * 480
    try:
        results = [
            feed(recorder, "microphone", pcm, 70_000_000, mic),
            feed(recorder, "system", pcm, system_pts, system),
        ]
        clock.now = 90_000_000
        if fail_second:
            append = session.append_native

            def failing(identifier, *args, **kwargs):
                if identifier == "system":
                    raise OSError("synthetic disk failure")
                return append(identifier, *args, **kwargs)

            monkeypatch.setattr(session, "append_native", failing)
        results += [
            feed(recorder, "system", pcm, system_pts + 10_000_000, system),
            feed(recorder, "microphone", pcm, 80_000_000, mic),
        ]
        if fail_second:
            return results, problems()
        assert problems() == []
        assert results == [Gst.FlowReturn.OK] * 4
        # One clock read per branch, at its first buffer; later buffers use the cached shift.
        assert clock.lookups == 2
        session.finish()
        me, others = session.manifest["tracks"]
        assert me["startOffsetMs"] == 70 and me["frames"] == 960 and not me["gaps"]
        # Only a drift-slaved source's stamps are clock observations.
        assert me["clockAnchors"] == [[0, 5 * NS + 70_000_000 - NS], [480, 5 * NS + 80_000_000 - NS]]
        assert "clockAnchors" not in others
        # D57/R77: only the re-anchored track says why, and the checkpoint keeps it.
        assert "clockSlaving" not in me
        saved = json.loads((session.directory / "session.json").read_text())
        assert saved["tracks"][1].get("clockSlaving") == others.get("clockSlaving")
        assert "clockSlaving" not in saved["tracks"][0]
        return others
    finally:
        recorder.pipeline = None
        session.finish()
        recorder.close()


def test_wrapped_first_input_stamp_keeps_its_exact_gstreamer_anchor(tmp_path, monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="cassini_talk.native_recorder"):
        others = start_with_a_wrapped_system_stamp(
            tmp_path, monkeypatch, pulse_source(10000, 48000), WRAPPED_NS + 60_000_000
        )
    # GStreamer's own stamp was 60 ms; the second buffer stays contiguous.
    assert others["frames"] == 2880 + 960
    assert others["gaps"] == [dict(atMs=0, durationMs=60, frames=2880, kind="prefix")]
    assert f"removed the exact 2^32-segment wrap, shift {WRAPPED_NS} ns" in caplog.text
    assert others["clockSlaving"] == dict(state="lost", path="exactWrap", shiftNs=WRAPPED_NS)
    assert "Native system input has no GStreamer drift slaving" in caplog.text


def test_clock_slaving_is_accepted_by_the_pinned_session_schema(tmp_path, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    others = start_with_a_wrapped_system_stamp(
        tmp_path, monkeypatch, pulse_source(10000, 48000), WRAPPED_NS + 60_000_000
    )
    (path,) = (tmp_path / "Library").rglob("session.json")
    saved = json.loads(path.read_text())
    assert saved["tracks"][1]["clockSlaving"] == others["clockSlaving"]
    schema = json.loads((FIXTURE.parents[1] / "schemas/session.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(saved)


@pytest.mark.parametrize(
    ("source", "system_pts", "prefix", "anchor"),
    [
        # Without pulsesrc facts, the 10 ms buffer itself is the latency.
        (None, WRAPPED_NS + 60_000_000, 3360, 70_000_000),
        # 1024 samples at 48 kHz: 2^32 segments are not whole nanoseconds; 21.333 ms latency.
        ((21333, 48000), 2**32 * 1024 * NS // 48000 + 60_000_000, 2816, 58_667_000),
        # Far ahead, but not by 2^32 segments; the reported 20 ms latency, not the 10 ms buffer.
        ((20000, 48000), 3600 * NS, 2880, 60_000_000),
    ],
    ids=["no-pulsesrc", "fractional-segment", "not-a-wrap"],
)
def test_unexplained_future_first_stamp_starts_one_source_latency_before_running_time(
    tmp_path, monkeypatch, caplog, source, system_pts, prefix, anchor
):
    source = pulse_source(*source) if source else None
    with caplog.at_level("WARNING", logger="cassini_talk.native_recorder"):
        others = start_with_a_wrapped_system_stamp(tmp_path, monkeypatch, source, system_pts)
    # The second buffer stays contiguous with the re-anchored first one.
    assert others["frames"] == prefix + 960
    assert [(gap["kind"], gap["frames"]) for gap in others["gaps"]] == [("prefix", prefix)]
    assert others["clockSlaving"] == dict(state="lost", path="runningTime", shiftNs=system_pts - anchor)
    assert "re-anchored at running time" in caplog.text
    assert "Native system input has no GStreamer drift slaving" in caplog.text


def test_shifted_source_failure_logs_the_raw_stamp_and_its_shift(tmp_path, monkeypatch, caplog):
    _NativeRecorder, Gst = recorder_class()
    with caplog.at_level("WARNING", logger="cassini_talk.native_recorder"):
        results, problems = start_with_a_wrapped_system_stamp(
            tmp_path, monkeypatch, pulse_source(10000, 48000), WRAPPED_NS + 60_000_000, fail_second=True
        )
    assert results[2] == Gst.FlowReturn.ERROR and problems == ["synthetic disk failure"]
    raw = WRAPPED_NS + 70_000_000
    assert f"Native system capture failed at raw input stamp {raw} ns (shift {WRAPPED_NS} ns)" in caplog.text


@pytest.mark.parametrize(
    ("latency_us", "rate", "wrap"),
    [(10000, 48000, WRAPPED_NS), (10000, 44100, WRAPPED_NS), (21333, 48000, None)],
)
def test_segment_wrap_is_exact_or_absent(latency_us, rate, wrap):
    recorder_class()  # Skips where GStreamer's Python bindings are absent.
    from cassini_talk.native_recorder import segment_wrap_ns

    assert segment_wrap_ns(latency_us, rate) == wrap


@pytest.mark.parametrize(("source", "facts"), [((10000, 48000), (10000, 48000)), ((0, 48000), None), (None, None)])
def test_pulse_latency_needs_a_negotiated_pulsesrc(source, facts):
    recorder_class()
    from cassini_talk.native_recorder import pulse_latency

    assert pulse_latency(Sink(pulse_source(*source) if source else None)) == facts


def test_native_buffer_beyond_the_wire_chunk_limit_is_split_not_rejected(tmp_path, monkeypatch):
    NativeRecorder, Gst = recorder_class()
    session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    recorder = NativeRecorder()
    recorder.session = session
    problems = scheduled_problems(monkeypatch, recorder)
    pcm = bytes(range(250)) * (3 * MAX_CHUNK // 500)
    try:
        result = feed(recorder, "microphone", pcm, 0)
        assert problems() == [] and result == Gst.FlowReturn.OK
        session.finish()
        (track,) = session.manifest["tracks"]
        assert track["frames"] == len(pcm) // 2 and not track["gaps"]
        with wave.open(str(session.directory / track["file"])) as audio:
            assert audio.readframes(audio.getnframes()) == pcm
    finally:
        session.finish()
        recorder.close()


def test_native_chunk_failure_names_its_source_and_condition(tmp_path, monkeypatch, caplog):
    NativeRecorder, Gst = recorder_class()
    session = RecordingSession(tmp_path)
    session.add_track("system", "The others", "system", 48000, 1)
    recorder = NativeRecorder()
    recorder.session = session
    problems = scheduled_problems(monkeypatch, recorder)
    try:
        with caplog.at_level("WARNING", logger="cassini_talk.native_recorder"):
            result = feed(recorder, "system", bytes(960), WRAPPED_NS)
        assert result == Gst.FlowReturn.ERROR
        assert problems() == ["Audio timestamp 42949672960.0 ms is outside 0..86400000"]
        assert "Native system capture failed" in caplog.text
    finally:
        session.finish()
        recorder.close()


@pytest.mark.parametrize(
    ("data", "offset", "message"),
    [
        (b"\0\0", "0", "Audio timestamp is not a number"),
        (b"\0\0", float("nan"), "Audio timestamp nan ms is outside 0..86400000"),
        (b"\0\0", -1, "Audio timestamp -1 ms is outside 0..86400000"),
        (b"\0\0", 10**400, "Audio timestamp beyond 1e18 ms is outside 0..86400000"),
        (b"", 0, "Empty audio chunk"),
        (bytes(MAX_CHUNK + 2), 0, "Audio chunk of 262146 bytes exceeds 262144"),
    ],
    ids=["type", "nan", "negative", "huge", "empty", "oversized"],
)
def test_chunk_errors_name_the_failing_condition(data, offset, message):
    with pytest.raises(ProtocolError) as error:
        chunk_plan(48000, 1, 0, None, data, offset)
    assert (error.value.code, str(error.value)) == ("chunk", message)


def test_append_names_an_unknown_or_finished_source(tmp_path):
    session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    with pytest.raises(ProtocolError, match="^Audio chunk for an unknown source$"):
        session.append("system", b"\0\0", 0)
    session.finish()
    with pytest.raises(ProtocolError, match="^Audio arrived after the recording finished$"):
        session.append("microphone", b"\0\0", 0)
