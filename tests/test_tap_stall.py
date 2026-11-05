# SPDX-License-Identifier: GPL-3.0-or-later
"""R93-2: a host stall must not drop native input, and Stop keeps what was captured.

Each native tap holds 10 s ahead of its synchronous writer (TAP_QUEUE), a
checkpoint fsyncs only the WAVs written since the last one, and Stop records
the audio a tap still queues from before the Stop moment. The real-pulsesrc
stall itself is tests/recording/stall.py; these cover each piece without a
sound server. The taps here are scripted: every wait is on an event the code
under test sets, with a bound only as an upper limit (E1).
"""

import os
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from cassini_talk import native_recorder, native_tap  # noqa: E402
from cassini_talk.recorder import TAP_QUEUE  # noqa: E402
from cassini_talk.recording_session import RecordingSession, WAVWriter  # noqa: E402

Gst.init(None)
BOUND = 10  # seconds; an upper limit only


class Described(Exception):
    pass


def tap_descriptions(monkeypatch):
    """Each shipped native tap's description, as it is parsed."""
    seen = []

    def record(description, *_):
        seen.append(description)
        raise Described

    with monkeypatch.context() as patch:
        patch.setattr(Gst, "parse_launch", record)
        patch.setattr(Gst, "parse_bin_from_description", record)
        for kind in ("microphone", "system"):
            with pytest.raises(Described):
                native_tap.NativeTap(kind, 0, lambda *_: None, lambda *_: None).start()
            with pytest.raises(Described):
                native_recorder.NativeRecorder._branch(SimpleNamespace(_test_sources=False), kind)
    return seen


def test_each_native_tap_queues_ten_seconds_in_c_ahead_of_its_writer(monkeypatch):
    descriptions = tap_descriptions(monkeypatch)
    assert len(descriptions) == 4
    for description in descriptions:
        # The queue sits right after the device, ahead of convert/resample/appsink.
        source, rest = description.split(" ! ", 1)
        assert source.startswith("pulsesrc ")
        assert rest.startswith(TAP_QUEUE + " ! ")
        pipeline = Gst.parse_launch("audiotestsrc ! " + rest.replace("appsink", "appsink async=false", 1))
        try:
            queue = pipeline.get_by_name("tap-queue")
            assert queue is not None
            assert queue.get_property("max-size-time") == 10 * Gst.SECOND
            # Both size caps off: 200 buffers or 10 MB would cut the 10 s short.
            assert queue.get_property("max-size-buffers") == 0
            assert queue.get_property("max-size-bytes") == 0
            assert queue.get_property("leaky").value_nick == "no"
        finally:
            pipeline.set_state(Gst.State.NULL)


class Fsyncs:
    """os.fsync, counted per file path."""

    def __init__(self, monkeypatch, fail=()):
        self.paths, self.fail = [], set(fail)
        real = os.fsync

        def fsync(fd):
            path = os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
            self.paths.append(path)
            if path in self.fail:
                self.fail.discard(path)
                raise OSError("Generated fsync failure")
            return real(fd)

        monkeypatch.setattr(os, "fsync", fsync)

    def take(self):
        paths, self.paths = self.paths, []
        return paths


def test_a_wav_writer_fsyncs_only_what_was_written_since_its_last_sync(tmp_path, monkeypatch):
    path = Path(os.path.realpath(tmp_path)) / "track.wav"
    fsyncs = Fsyncs(monkeypatch)
    writer = WAVWriter(path, 48000, 1)
    writer.sync()  # The header written at creation is not durable yet.
    assert fsyncs.take() == [str(path)]
    writer.sync()
    assert fsyncs.take() == []
    writer.append(bytes(960))
    writer.sync()
    assert fsyncs.take() == [str(path)]
    writer.silence(480)
    writer.sync()
    writer.sync()
    assert fsyncs.take() == [str(path)]
    # A failed fsync stays owed: the next sync retries it and fails again.
    fsyncs.fail.add(str(path))
    writer.append(bytes(960))
    with pytest.raises(OSError):
        writer.sync()
    writer.sync()
    assert fsyncs.take() == [str(path), str(path)]
    writer.close()  # Clean: nothing to make durable.
    assert fsyncs.take() == [] and writer.file.closed
    assert path.stat().st_size == 44 + 3 * 960


def test_a_checkpoint_fsyncs_an_ended_track_once_and_then_never_again(tmp_path, monkeypatch):
    session = RecordingSession(tmp_path, source="gnome", clock=lambda: 0)
    try:
        session.add_track("microphone", "Me", "microphone", 48000, 1)
        session.add_track("system", "The others", "system", 48000, 1)
        session.append_native("microphone", bytes(960), 0)
        session.append_native("system", bytes(960), 0)
        microphone, system = (os.path.realpath(session.writers[i].file.name) for i in ("microphone", "system"))
        fsyncs = Fsyncs(monkeypatch)
        session.end_track("system")  # Its checkpoint makes its last frames durable.
        first = fsyncs.take()
        assert first.count(system) == 1 and first.count(microphone) == 1
        for _ in range(3):
            session.append_native("microphone", bytes(960), session.writers["microphone"].frames / 48)
            session.checkpoint()
            # The active WAV, session.tmp and the directory; never the ended WAV.
            paths = fsyncs.take()
            assert paths.count(microphone) == 1 and system not in paths
            assert len(paths) == 3
        session.checkpoint()  # Nothing written: only the manifest and the directory.
        assert microphone not in fsyncs.take()
    finally:
        session.finish()
    assert session.manifest["state"] == "completed"
    assert [t["frames"] for t in session.manifest["tracks"]] == [4 * 480, 480]


# Stop keeps the audio captured before it (R93-2, D47).


class ManualClock:
    """A SessionClock whose now() the test sets: the Stop boundary is exact."""

    def __init__(self, origin_ms):
        self.origin_ms, self.ms = origin_ms, 0.0

    def now(self):
        return self.ms

    def at(self, _monotonic_ns):
        return self.ms


class DrainingTaps:
    """Scripted taps. A heard tap delivers readiness PCM at its open; a silent
    one is armed (D58.1). stop() is the EOS drain: it reports that it began and
    returns when the test has delivered what the tap still queued."""

    def __init__(self, silent=()):
        self.silent = set(silent)
        self.taps = {}

    def __call__(self, kind, _clock, append, failed, *, level):
        taps = self

        class Tap:
            pipeline = None

            def __init__(self):
                self.kind, self.append = kind, append
                self.draining, self.drained = threading.Event(), threading.Event()

            def start(self):
                if kind not in taps.silent:
                    self.append(bytes(4), 0)  # Readiness PCM before commit.

            def stop(self):
                self.draining.set()
                assert self.drained.wait(BOUND)  # Bound only: the test ends the drain.

        tap = Tap()
        self.taps[kind] = tap
        return tap


def start_capture(tmp_path, taps, *, microphone, others_source):
    from cassini_talk.browser_capture import BrowserCapture

    origin = int(time.time() * 1000)
    clock = ManualClock(origin)
    capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=taps)
    message = dict(type="start", captureId="stall", timelineOriginMs=origin, microphone=microphone)
    if others_source == "system":
        capture.browser_lost = True
        capture.start(message, others_source="system", reason="missing", session_clock=clock)
    else:
        capture.start(message, session_clock=clock)
    return capture, clock


def tone(frames, value=7):
    return struct.pack("<h", value) * frames


def stop_in_background(capture, tap):
    """Press Stop; returns once Stop's boundary is fixed and tap's drain began."""
    result = {}

    def run():
        try:
            result["stopped"] = capture.stop()
        except BaseException as error:  # pragma: no cover - reported by the join below
            result["error"] = error

    thread = threading.Thread(target=run, name="test-stop")
    thread.start()
    assert capture.stop_requested.wait(BOUND)
    assert tap.draining.wait(BOUND)
    return thread, result


def test_stop_records_the_queued_audio_captured_before_it_and_cuts_the_straddling_buffer(tmp_path):
    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    identifier, tap = capture.native_taps["microphone"]
    session = capture.host.session
    track = next(t for t in session.manifest["tracks"] if t["id"] == identifier)
    start = track["startOffsetMs"]
    # The device delivers 10 ms buffers back to back; 600 ms reach the WAV live.
    for index in range(60):
        tap.append(tone(480), start + index * 10)
    assert session.writers[identifier].frames == 60 * 480
    # Stop 345 ms later. The tap's queue still holds the 34 buffers captured
    # since, one straddling the Stop moment, and two captured after it.
    clock.ms = start + 945
    thread, result = stop_in_background(capture, tap)  # The Stop boundary is clock.ms.
    for index in range(60, 97):
        tap.append(tone(480, value=9), start + index * 10)
    tap.drained.set()
    thread.join(BOUND)
    assert not thread.is_alive() and "error" not in result
    saved = session.manifest
    (track,) = saved["tracks"]
    # Everything before the boundary, and the first 5 ms of the straddling buffer.
    assert track["frames"] == 94 * 480 + 240
    assert track["endOffsetMs"] == pytest.approx(start + 945)
    assert "gapFrames" not in track and not [g for g in track.get("gaps", []) if g["kind"] != "prefix"]
    assert saved["state"] == "completed" and saved.get("problemKind") is None
    assert saved["events"][-1] == dict(type="end", atMs=pytest.approx(start + 945), reason="user")
    pcm = (capture.directory / track["file"]).read_bytes()[44:]
    assert pcm[: 60 * 960] == tone(60 * 480) and pcm[60 * 960 :] == tone(34 * 480 + 240, value=9)


@pytest.mark.parametrize("first_ms, opens", [(-20, True), (-5, True), (0, False), (10, False)])
def test_an_armed_tap_whose_first_buffer_was_queued_before_stop_opens_its_track(tmp_path, first_ms, opens):
    """D58.1: a silent system tap is armed. Its first buffer, captured before
    Stop but still queued behind a stall, opens its track during the drain."""
    taps = DrainingTaps(silent={"system"})
    capture, clock = start_capture(tmp_path, taps, microphone=False, others_source="system")
    identifier, tap = capture.native_taps["system"]
    assert capture.snapshot()["armed"] == {"system": capture._tap_states[identifier]["at"]}
    clock.ms = capture._tap_states[identifier]["at"] + 2000
    boundary = clock.ms  # Stop's moment, on the capture's one clock.
    thread, result = stop_in_background(capture, tap)
    tap.append(tone(480), boundary + first_ms)
    tap.append(tone(480), boundary + first_ms + 10)
    tap.drained.set()
    thread.join(BOUND)
    assert not thread.is_alive() and "error" not in result
    assert capture.nothing_recorded is not opens
    if opens:
        (track,) = capture.host.session.manifest["tracks"]
        assert track["startOffsetMs"] == pytest.approx(boundary + first_ms)
        assert track["endOffsetMs"] == pytest.approx(boundary)
        assert result["stopped"]["path"] is not None
    else:
        assert result["stopped"]["path"] is None and capture.discarded


def test_a_buffer_after_the_session_finished_is_never_written(tmp_path):
    """Before Stop's moment, but after the save: a late callback (the pipeline not
    yet gone) writes nothing, and an armed tap's first buffer opens nothing."""
    taps = DrainingTaps(silent={"system"})
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="system")
    microphone, tap = capture.native_taps["microphone"]
    system = taps.taps["system"]
    session = capture.host.session
    start = session.manifest["tracks"][0]["startOffsetMs"]
    tap.append(tone(480), start)
    clock.ms = start + 1000
    tap.drained.set()
    system.drained.set()
    capture.stop()
    assert session.finished
    tap.append(tone(480), start + 10)
    system.append(tone(480), start + 10)
    assert session.writers[microphone].frames == 480
    assert [t["kind"] for t in session.manifest["tracks"]] == ["microphone"]
