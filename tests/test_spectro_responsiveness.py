# SPDX-License-Identifier: GPL-3.0-or-later
"""Blocked producers must never hold the window frame/hide/remap path."""

import ctypes.util
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace

import pytest
from test_spectro_analysis import tone
from test_spectro_source import feed

from cassini_talk import spectro_analysis
from cassini_talk.spectro_source import SpectroSource


@pytest.fixture(autouse=True)
def real_fft():
    if ctypes.util.find_library("gstfft-1.0") is None:
        pytest.skip("Install GStreamer base for the recording/GUI gate")


@pytest.mark.parametrize("phase", ["loader", "fft", "quantizer"])
def test_frame_hide_and_remap_finish_while_the_producer_is_blocked(monkeypatch, phase):
    pytest.importorskip("gi")
    from test_spectro_views import Strip, Window

    from cassini_talk.spectro_views import NativeFeed, WindowSpectrograms

    source = SpectroSource()
    recorder = SimpleNamespace(spectrogram=source, _start_us=0)
    first, second = Window(), Window()
    first_strip, second_strip = Strip(), Strip()
    first_hub, second_hub = WindowSpectrograms(first), WindowSpectrograms(second)
    first_hub.bind(first_strip, "one", lambda callback: NativeFeed(recorder, callback, clock=lambda: 100))
    entered, release = threading.Event(), threading.Event()
    cls, method = {
        "loader": (spectro_analysis.Track, "__init__"),
        "fft": (spectro_analysis.FFT, "powers"),
        "quantizer": (spectro_analysis.Quantizer, "column"),
    }[phase]
    original = getattr(cls, method)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(4), "test did not release the blocked producer"
        return original(*args, **kwargs)

    monkeypatch.setattr(cls, method, paused)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            producer = executor.submit(feed, source, "mic", kind="microphone")
            try:
                assert entered.wait(3), f"producer did not reach {phase} barrier"

                def gtk_events():
                    first.frame(100)  # Real NativeFeed.sample -> drain, via the window callback.
                    first_hub.close()  # Window hiding must not wait for analysis/loader I/O.
                    second_hub.bind(
                        second_strip, "one", lambda callback: NativeFeed(recorder, callback, clock=lambda: 500)
                    )
                    second.frame(500)

                gtk = executor.submit(gtk_events)
                try:
                    gtk.result(timeout=1)
                except TimeoutError:
                    pytest.fail(f"GTK frame/hide/remap waited on the {phase} barrier")
                assert not release.is_set() and not producer.done()
                assert not first.ticks and first_strip.model is None
                assert len(source.subscribers) == 1
            finally:
                release.set()
            producer.result(timeout=3)
        new_feed = second_hub.sessions["one"].feed
        assert not any(source.drain(new_feed.subscription, 500).values()), "in-flight old columns crossed remap"
        feed(source, "mic", offset=500, now=600, kind="microphone")
        columns = source.drain(new_feed.subscription, 600)["me"]
        assert columns and min(row[0] for row in columns) >= 25
    finally:
        release.set()
        first_hub.close()
        second_hub.close()


def test_drain_and_cancel_do_not_wait_even_for_the_short_publication_lock():
    source = SpectroSource()
    subscription = source.subscribe()
    entered, release = threading.Event(), threading.Event()

    def publisher():
        with source.queue_lock:
            entered.set()
            assert release.wait(4), "test did not release publication lock"

    with ThreadPoolExecutor(max_workers=2) as executor:
        publishing = executor.submit(publisher)
        try:
            assert entered.wait(3), "publication barrier was not reached"

            def reader():
                assert not any(source.drain(subscription, 100).values())
                source.unsubscribe(subscription)
                assert not source.active and not source.subscribers

            pending = executor.submit(reader)
            try:
                pending.result(timeout=1)
            except TimeoutError:
                pytest.fail("GTK drain/cancellation waited on publication")
            assert not release.is_set()
        finally:
            release.set()
        publishing.result(timeout=3)
    source.feed("mic", tone(4800), 0, rate=48000, channels=1, kind="microphone", start_ms=0, now_ms=100, level=0.5)
    assert source.calls == source.analysed == 0
