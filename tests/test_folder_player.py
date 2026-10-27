# SPDX-License-Identifier: GPL-3.0-or-later
"""Real offline GStreamer output; fakesink/appsink, never an audio device."""

import array
import math
import time
import wave

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library import CaptureTrack  # noqa: E402


def tone(path, frequency, seconds=2, rate=48000, channels=1):
    samples = array.array(
        "h",
        (
            round(4000 * math.sin(2 * math.pi * frequency * i / rate))
            for i in range(round(seconds * rate))
            for _channel in range(channels)
        ),
    )
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    return round(seconds * rate)


def until(predicate, timeout=5):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "GStreamer did not reach the expected state"


def capture(tracks, seek=None):
    sink = Gst.ElementFactory.make("appsink", None)
    sink.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=1"))
    sink.set_property("emit-signals", True)
    sink.set_property("sync", False)
    chunks, pts, errors = [], [], []

    def received(src):
        sample = src.emit("pull-sample")
        buffer = sample.get_buffer()
        chunks.append(buffer.extract_dup(0, buffer.get_size()))
        pts.append(buffer.pts)
        return Gst.FlowReturn.OK

    sink.connect("new-sample", received)
    player = FolderPlayer(audio_sink=sink)
    player.connect("error", lambda _p, message: errors.append(message))
    try:
        player.load_tracks(tracks)
        until(lambda: player._prerolled or errors)
        assert not errors
        if seek is not None:
            player.seek(seek)
            until(lambda: player._pending_seek is None or errors)
        player.play()
        until(lambda: player._at_end or errors)
        assert not errors
        result = array.array("h", b"".join(chunks))
        return result, pts, player.duration
    finally:
        player.shutdown()


def level(samples, start, stop, frequency):
    values = samples[round(start * 48000) : round(stop * 48000)]
    n = len(values)
    re = sum(v * math.cos(2 * math.pi * frequency * i / 48000) for i, v in enumerate(values))
    im = sum(v * math.sin(2 * math.pi * frequency * i / 48000) for i, v in enumerate(values))
    return 2 * math.hypot(re, im) / n


def test_two_sources_obey_offsets_and_global_seek(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    frames = tone(a, 440)
    other = tone(b, 880, seconds=1)
    tracks = [CaptureTrack(a, frames, 48000, 0, True), CaptureTrack(b, other, 48000, 500, True)]
    samples, pts, duration = capture(tracks)
    assert duration == 2000
    assert abs(len(samples) - 96000) < 480
    assert level(samples, 0.1, 0.4, 440) > 3500
    assert level(samples, 0.1, 0.4, 880) < 50
    assert level(samples, 0.6, 1.3, 440) > 3500
    assert level(samples, 0.6, 1.3, 880) > 3500
    assert level(samples, 1.6, 1.9, 880) < 50
    samples, pts, duration = capture(tracks, seek=750)
    assert duration == 2000 and abs(len(samples) - 60000) < 480
    assert pts[0] >= 750 * Gst.MSECOND
    assert level(samples, 0.1, 0.5, 880) > 3500
    assert level(samples, 0.9, 1.1, 880) < 50


def test_unknown_offsets_are_not_aligned_as_zero(tmp_path):
    path = tmp_path / "unknown.wav"
    frames = tone(path, 440, seconds=0.1)
    player = FolderPlayer(audio_sink=Gst.ElementFactory.make("fakesink", None))
    try:
        with pytest.raises(ValueError, match="unknown start offset"):
            player.load_tracks([CaptureTrack(path, frames, 48000, None, True)])
        assert player.sources == []
    finally:
        player.shutdown()


def test_main_fixture_plays_and_seeks_without_decoding_the_whole_meeting(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from cassini_viewer.library import scan_library

    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "ux/library-vectors/make.py"), str(tmp_path / "fixture")], check=True)
    meeting = next(m for m in scan_library(tmp_path / "fixture/main/root").meetings if m.title == "Budget review")
    assert FolderPlayer.can_play(meeting) and meeting.duration_ms == 3120000
    samples, pts, duration = capture(meeting.tracks, seek=3119000)
    assert duration == 3120000 and abs(len(samples) - 48000) < 480
    assert pts[0] >= 3119000 * Gst.MSECOND and max(map(abs, samples)) == 0


def test_sources_open_off_main_thread_and_resample(tmp_path, monkeypatch):
    import os
    import threading

    path = tmp_path / "8k.wav"
    frames = tone(path, 440, seconds=1, rate=8000)
    main = threading.get_ident()
    original = os.open
    threads = []

    def opened(filename, *args, **kwargs):
        if filename == path:
            threads.append(threading.get_ident())
            assert threads[-1] != main
        return original(filename, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    samples, _pts, duration = capture([CaptureTrack(path, frames, 8000, 100, True)])
    assert threads and duration == 1100
    assert abs(len(samples) - 52800) < 480
    assert level(samples, 0.2, 0.9, 440) > 3500
    assert max(map(abs, samples[:4000])) == 0


def test_missing_source_reports_bus_error_and_cleans_up(tmp_path):
    sink = Gst.ElementFactory.make("fakesink", None)
    player = FolderPlayer(audio_sink=sink)
    errors = []
    player.connect("error", lambda _p, message: errors.append(message))
    try:
        player.load_tracks([CaptureTrack(tmp_path / "missing.wav", 100, 8000, 0, True)])
        player.play()
        until(lambda: bool(errors))
        assert not player.playing
    finally:
        player.shutdown()
    assert player.sources == [] and player._pipeline is None


def test_mono_and_stereo_sources_with_different_rates_mix_together(tmp_path):
    a, b = tmp_path / "8k.wav", tmp_path / "44k-stereo.wav"
    frames = tone(a, 440, seconds=1, rate=8000)
    other = tone(b, 880, seconds=0.5, rate=44100, channels=2)
    samples, _pts, duration = capture(
        [CaptureTrack(a, frames, 8000, 0, True), CaptureTrack(b, other, 44100, 250, True, 2)]
    )
    assert duration == 1000 and abs(len(samples) - 48000) < 480
    assert level(samples, 0.3, 0.6, 440) > 3500 and level(samples, 0.3, 0.6, 880) > 3500
    assert level(samples, 0.8, 0.95, 880) < 50


def test_unloaded_and_rejected_folder_load_cannot_play_old_audio(tmp_path):
    player = FolderPlayer(audio_sink=Gst.ElementFactory.make("fakesink", None))
    errors = []
    player.connect("error", lambda _p, message: errors.append(message))
    try:
        player.play()
        assert errors and not player.playing and not player.loaded
        path = tmp_path / "valid.wav"
        frames = tone(path, 440, seconds=0.1)
        player.load_tracks([CaptureTrack(path, frames, 48000, 0, True)])
        assert player.loaded
        with pytest.raises(ValueError):
            player.load_tracks([CaptureTrack(path, frames, 48000, None, True)])
        player.play()
        assert not player.loaded and not player.playing and player._tick_id == 0
    finally:
        player.shutdown()


def test_folder_state_failure_does_not_mark_playing(tmp_path):
    path = tmp_path / "valid.wav"
    frames = tone(path, 440, seconds=0.1)
    player = FolderPlayer(audio_sink=Gst.ElementFactory.make("fakesink", None))
    errors = []
    player.connect("error", lambda _p, message: errors.append(message))
    try:
        player.load_tracks([CaptureTrack(path, frames, 48000, 0, True)])
        original = player._pipeline.set_state
        player._pipeline.set_state = lambda state: (
            Gst.StateChangeReturn.FAILURE if state == Gst.State.PLAYING else original(state)
        )
        player.play()
        assert errors and not player.playing and player._tick_id == 0
    finally:
        player.shutdown()
