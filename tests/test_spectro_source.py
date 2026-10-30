# SPDX-License-Identifier: GPL-3.0-or-later
"""Real DSP for independent observers, multi-track power and bounded history."""

import ctypes.util
import math
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_spectro_analysis import tone

from cassini_viewer.spectro_source import SpectroSource


@pytest.fixture(autouse=True)
def real_fft():
    if ctypes.util.find_library("gstfft-1.0") is None:
        pytest.skip("Install GStreamer base for the required recording/GUI gate")


def feed(source, identifier, *, offset=0, now=100, frames=4800, level=0.3, kind="remote"):
    source.feed(
        identifier,
        tone(frames, amplitude=500),
        offset,
        rate=48000,
        channels=1,
        kind=kind,
        start_ms=0,
        now_ms=now,
        level=level,
    )


def test_no_observer_means_no_conversion_fft_state_or_counters():
    source = SpectroSource()
    feed(source, "one")
    assert (source.calls, source.analysed, source.analysis_ns) == (0, 0, 0)
    assert not source.tracks
    sub = source.subscribe()
    feed(source, "one")
    assert source.drain(sub, 100)["others"]
    source.unsubscribe(sub)
    before = source.calls, source.analysed, source.analysis_ns
    feed(source, "one", offset=100, now=200)
    assert (source.calls, source.analysed, source.analysis_ns) == before
    assert not source.tracks and not any(source.pending.values())
    sub = source.subscribe()
    assert not any(source.drain(sub, 200).values())


def test_two_identical_tracks_mix_as_power_before_quantisation():
    solo, mixed = SpectroSource(), SpectroSource()
    a, b = solo.subscribe(), mixed.subscribe()
    feed(solo, "one")
    feed(mixed, "one")
    feed(mixed, "two")
    raw, both = solo.drain(a, 100)["others"], mixed.drain(b, 100)["others"]
    assert [r[0] for r in raw] == [r[0] for r in both]
    # +3.0103 dB is about 13 of the 0.2353-dB fixed-scale codes.
    assert all(12 <= max(y[2]) - max(x[2]) <= 13 for x, y in zip(raw, both, strict=True))


def test_two_observers_drain_independently_and_only_last_close_drops_history():
    source = SpectroSource()
    a, b = source.subscribe(), source.subscribe()
    feed(source, "one")
    expected = source.drain(a, 100)
    assert source.drain(b, 100) == expected
    assert not any(source.drain(a, 100).values())
    source.unsubscribe(a)
    assert source.tracks
    feed(source, "one", offset=100, now=200)
    assert source.drain(b, 200)["others"]
    source.unsubscribe(b)
    assert not source.tracks


def test_each_lane_chooses_only_three_tracks_once_per_second_and_retires():
    source = SpectroSource()
    sub = source.subscribe()
    for n in range(5):
        feed(source, str(n), level=n / 10)
    assert len(source.selected) == 3
    feed(source, "4", offset=100, now=1100, level=0.9)
    assert source.selected == {"2", "3", "4"}
    assert source.tracks["0"].analysis.total == 4800
    source.retire("3")
    assert "3" not in source.selected and "3" not in source.tracks
    assert source.drain(sub, 1100)["others"]


def test_a_missing_remote_track_does_not_freeze_a_continuing_lane():
    source = SpectroSource()
    sub = source.subscribe()
    feed(source, "one")
    feed(source, "two")
    source.drain(sub, 100)
    feed(source, "one", offset=100, now=500)
    rows = source.drain(sub, 500)["others"]
    assert rows and rows[-1][0] >= 8


def test_queue_and_pending_slots_are_bounded_for_a_slow_view():
    source = SpectroSource()
    sub = source.subscribe()
    for n in range(30):
        feed(source, "one", offset=n * 100, now=(n + 1) * 100)
    assert len(source.pending["others"]) <= 64
    assert len(source.drain(sub, 3000)["others"]) <= 64
    assert source.overflow > 0


def test_parallel_native_inputs_keep_their_lanes_independent():
    source = SpectroSource()
    sub = source.subscribe()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(feed, source, kind, kind=kind) for kind in ("microphone", "system")]
        for future in futures:
            future.result(timeout=3)
    rows = source.drain(sub, 100)
    assert rows["me"] == rows["others"] and rows["me"]
    assert math.isfinite(source.analysis_ns) and source.late == 0
