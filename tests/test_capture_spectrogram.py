# SPDX-License-Identifier: GPL-3.0-or-later
"""The picture observes committed browser/native PCM outside capture locks."""

import base64
import ctypes.util
import struct
import time
import wave

import pytest
from test_browser_capture import Tap
from test_spectro_analysis import tone

from cassini_talk.browser_capture import BrowserCapture


@pytest.fixture(autouse=True)
def real_fft():
    if ctypes.util.find_library("gstfft-1.0") is None:
        pytest.skip("Install GStreamer base for the recording/GUI gate")


def capture_at(root, microphone=False, others_source="browser"):
    capture = BrowserCapture(lambda *_: None, parent=root, tap_factory=Tap)
    capture.start(
        dict(
            type="start",
            version=1,
            independent=True,
            captureId="spectrum",
            timelineOriginMs=int(time.time() * 1000),
            microphone=microphone,
        ),
        others_source=others_source,
        reason="missing" if others_source == "system" else "start",
    )
    return capture


def packet(capture, pcm, sequence=0, offset=0):
    return capture.handle(
        dict(type="audio", id="wire", sequence=sequence, offsetMs=offset, pcm=base64.b64encode(pcm).decode())
    )


def declare(capture, kind="remote", channels=1):
    capture.handle(dict(type="track", id="wire", label="Other", kind=kind, sampleRate=48000, channels=channels))


def read_pcm(capture, identifier):
    track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
    with wave.open(str(capture.directory / track["file"])) as audio:
        return audio.readframes(audio.getnframes())


@pytest.mark.parametrize("kind", ["remote", "application"])
@pytest.mark.parametrize("channels", [1, 2])
def test_browser_pcm_is_unchanged_and_analysis_is_outside_the_owner_lock(tmp_path, monkeypatch, kind, channels):
    capture = capture_at(tmp_path)
    source = capture.spectrogram
    subscription = source.subscribe()
    calls, observe = [], source.observe

    def outside(*args, **kwargs):
        assert not capture.lock._is_owned(), "DSP ran under the recording/selection lock"
        calls.append(kwargs)
        return observe(*args, **kwargs)

    monkeypatch.setattr(source, "observe", outside)
    pcm = tone(4800, channels=channels, amplitude=500)
    try:
        declare(capture, kind, channels)
        packet(capture, pcm)
        assert len(calls) == 1 and calls[0]["data"] == pcm
        assert source.drain(subscription, 100)["others"]
        source.unsubscribe(subscription)
        before = source.calls
        packet(capture, pcm, 1, 100)
        assert source.calls == before and len(calls) == 1 and capture.streams.picture is None
        assert not source.tracks, "hidden producer retained FFT/history after accepting new PCM"
        capture.stop()
        assert read_pcm(capture, "wire") == pcm + pcm
    finally:
        capture.stop()


@pytest.mark.parametrize("kind", ["microphone", "system"])
def test_broker_native_and_readiness_taps_keep_audio_independent_of_picture(tmp_path, monkeypatch, kind):
    capture = capture_at(
        tmp_path, microphone=kind == "microphone", others_source="system" if kind == "system" else "browser"
    )
    source = capture.spectrogram
    source.subscribe()
    observed, observe = [], source.observe

    def outside(*args, **kwargs):
        assert not capture.lock._is_owned()
        observed.append(kwargs)
        return observe(*args, **kwargs)

    monkeypatch.setattr(source, "observe", outside)
    pcm = tone(4800, amplitude=600)
    try:
        preview, _state = capture._make_native("system", "not-committed")
        preview.start()
        assert not observed, "Readiness PCM reached the analyser"
        identifier, tap = capture.native_taps[kind]
        tap.append(pcm, 100 + 4 * 1000 / 48000)
        assert len(observed) == 1 and observed[0]["kind"] == kind
        assert observed[0]["start_ms"] == (100 if kind == "microphone" else 0)
        source.unsubscribe(next(iter(source.subscribers)))
        tap.append(pcm, 200 + 4 * 1000 / 48000)
        assert not source.tracks and len(observed) == 1
        capture.stop()
        prefix = bytes(9600) if kind == "system" else b""
        assert read_pcm(capture, identifier) == prefix + struct.pack("<hhhh", 100, 200, 300, 400) + pcm + pcm
    finally:
        capture.stop()


def test_analysis_failure_does_not_reject_a_wire_packet_or_change_saved_pcm(tmp_path, monkeypatch):
    capture = capture_at(tmp_path)
    capture.spectrogram.subscribe()

    def failure(*_args, **_kwargs):
        raise OSError("Synthetic picture failure")

    monkeypatch.setattr(capture.spectrogram, "feed", failure)
    pcm = tone(4800, amplitude=300)
    try:
        declare(capture)
        assert packet(capture, pcm)["type"] == "ack"
        assert capture.spectrogram.problem == "Synthetic picture failure"
        assert not capture.finished and capture.error is None
        packet(capture, pcm, 1, 100)
        capture.stop()
        assert read_pcm(capture, "wire") == pcm + pcm
    finally:
        capture.stop()


def test_deselected_wire_pcm_is_not_analysed_and_new_span_is_declared(tmp_path):
    capture = capture_at(tmp_path)
    capture.spectrogram.subscribe()
    pcm = tone(4800, amplitude=300)
    try:
        declare(capture)
        packet(capture, pcm)
        capture.select(True, False)
        assert "wire" in capture.spectrogram.retired
        before = capture.spectrogram.calls
        packet(capture, pcm, 1, 100)
        assert capture.spectrogram.calls == before
        capture.select(True, True)
        identifier = capture.streams.tracks["wire"].recorded
        assert identifier != "wire" and identifier in capture.spectrogram.known
        packet(capture, pcm, 2, 200)
        assert capture.spectrogram.calls > before
    finally:
        capture.stop()
