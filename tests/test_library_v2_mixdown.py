# SPDX-License-Identifier: GPL-3.0-or-later
"""S-12 §4.3/§4.4 and §9.1/§9.4: timeline mapping, limiter, alignment golden, long meeting."""

import array
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")

from library_v2_support import (  # noqa: E402
    RATE,
    click,
    decoder_for,
    marker_at,
    new_session,
    rms,
    track_audio,
    write_wav_blocks,
)

from cassini_viewer import portable  # noqa: E402
from cassini_viewer.export_audio import limited_pcm  # noqa: E402
from cassini_viewer.library_v2 import finalize, mixdown, ogg  # noqa: E402

FRAME = mixdown.FRAME


def span(start_ms=0.0, anchors=(), rate=RATE, frames=RATE):
    from cassini_viewer.track_source import SpanInfo

    info = SpanInfo("pcm", ("track-x.wav",), frames, rate, 1, frames, rate)
    return mixdown.Span("x", Path("/nonexistent"), info, start_ms, tuple(anchors))


def test_limiter_matches_the_export_limiter_sample_for_sample():
    rng = np.random.default_rng(7)
    signal = rng.standard_normal(20000) * 0.4
    signal[5000:5300] *= 4  # peaks well above 0.95
    signal[12000] = 3.0
    expected = np.frombuffer(
        b"".join(
            a.tobytes() for a in limited_pcm([array.array("f", signal[i : i + 3001]) for i in range(0, 20000, 3001)])
        ),
        dtype=np.float32,
    )
    limiter = mixdown.Limiter()
    got = np.concatenate(
        [
            x
            for x in mixdown.limited(
                [signal[i : i + 4800].astype(np.float32).astype(np.float64) for i in range(0, 20000, 4800)], limiter
            )
        ]
    )
    assert len(got) == len(expected) == 20000
    np.testing.assert_allclose(got.astype(np.float32), expected, atol=2e-6)
    assert np.abs(got).max() <= 0.95 + 1e-6


@pytest.mark.parametrize("length", [1, 100, 240, 241, 4799])
def test_limiter_preserves_short_lengths_and_quiet_sound(length):
    quiet = np.full(length, 0.1)
    out = np.concatenate(list(mixdown.limited([quiet])))
    np.testing.assert_allclose(out, quiet)
    with pytest.raises(ValueError):
        mixdown.Limiter().process(np.array([0.0, np.nan]))


def test_nominal_mapping_is_an_integer_shift_applied_once():
    mapping = mixdown.Mapping(span(start_ms=1000.0104))
    assert mapping.knots is None and mapping.shift == 48000
    assert mapping.end(48000) == 96000
    assert mixdown.total_samples([span(), span(start_ms=2000, frames=10)]) == 96010


def test_anchors_map_piecewise_linearly_and_extrapolate_with_the_nearest_valid_slope():
    fast = 1 + 100e-6  # the source clock runs 100 ppm fast: more samples per true second
    anchors = [(round(t * RATE * fast), t * 10**9) for t in (0, 60, 120)]
    anchors += [(round(130 * RATE * fast), 125 * 10**9), (5, 10**9)]  # one bad slope, one out of order
    mapping = mixdown.Mapping(span(start_ms=500, anchors=anchors, frames=200 * RATE))
    assert mapping.knots is not None and len(mapping.knots[0]) == 3
    for t in (0, 30, 120, 180):  # inside and past the last anchor
        assert abs(mapping.position([t * RATE * fast])[0] - (500 * 48 + t * RATE)) < 1
    back = mapping.source(np.array([500 * 48 + 90 * RATE]))
    assert abs(back[0] - 90 * RATE * fast) < 1
    # One anchor is not enough: the nominal rate applies.
    assert mixdown.Mapping(span(anchors=anchors[:1])).knots is None


def test_x_tracks_keep_the_span_map_and_zero_sample_sources_are_reported(tmp_path):
    audio = track_audio(RATE, 1.0)
    tracks = [
        dict(id="mic", kind="microphone", file="track-mic.wav", sampleRate=RATE, channels=1, frames=999999,
             startOffsetMs=0, gapFrames=48000, write={"track-mic.wav": (audio, RATE, 1)}),
        dict(id="tab", kind="tab", sourceTrackId="t1", file="track-tab.wav", sampleRate=RATE, channels=1, frames=0,
             startOffsetMs=None, write={"track-tab.wav": (np.zeros(0), RATE, 1)}),
    ]  # fmt: skip
    _sid, directory = new_session(tmp_path, tracks)
    timeline = mixdown.session_spans(directory, json.loads((directory / "session.json").read_text()))
    assert [s.track for s in timeline.spans] == ["mic"] and timeline.empty == ["tab"]
    assert timeline.spans[0].info.frames == RATE  # frames on disk, not the stale session.json count (G25)
    assert timeline.tracks == [
        dict(id="mic", kind="microphone", spans=[dict(file="track-mic.wav", sampleRate=RATE, channels=1, frames=RATE,
             startOffsetMs=0, gapMaterialized=True)]),
        dict(id="tab", kind="tab", sourceTrackId="t1", spans=[dict(file="track-tab.wav", sampleRate=RATE, channels=1,
             frames=0, startOffsetMs=None)]),
    ]  # fmt: skip


def test_stereo_spans_are_averaged_and_tracks_summed_without_normalisation(tmp_path):
    left = np.full(RATE, 0.3)
    right = np.full(RATE, 0.1)
    other = np.full(RATE, 0.25)
    tracks = [
        dict(id="a", kind="system", file="track-a.wav", sampleRate=RATE, channels=2, frames=RATE, startOffsetMs=0,
             write={"track-a.wav": (np.stack([left, right], axis=1), RATE, 2)}),
        dict(id="b", kind="microphone", file="track-b.wav", sampleRate=RATE, channels=1, frames=RATE, startOffsetMs=0,
             write={"track-b.wav": (other, RATE, 1)}),
    ]  # fmt: skip
    _sid, directory = new_session(tmp_path, tracks)
    timeline = mixdown.session_spans(directory, json.loads((directory / "session.json").read_text()))
    mixed = np.concatenate(list(mixdown.mixed(timeline.spans, RATE)))
    np.testing.assert_allclose(mixed[1000:-1000], 0.2 + 0.25, atol=1e-3)


def published_samples(path):
    with open(path, "rb") as stream:
        return portable.compute_audio_digest(stream)["sampleCount"]


def test_alignment_golden(tmp_path):
    """§9.1: spans at 0 s and 60 min plus a late joiner, gaps (materialized and not), rotation, markers.

    A (0 s, 48 kHz): one PCM file with a materialized 4 s gap (10-14 s), then rotated into a second
    file that continues at 20 s. B (60 min, 48 kHz): an unmaterialized gap, two spans 3606-3610 s
    apart. C (late joiner at 5 min, 16 kHz stereo): a materialized gap and a rotated second span.
    """
    a1 = track_audio(RATE, 20, clicks=[2, 8, 16], silent=[(10, 14)])
    a2 = track_audio(RATE, 10, clicks=[5])
    b1 = track_audio(RATE, 6, clicks=[1])
    b2 = track_audio(RATE, 5, clicks=[2])
    c1 = track_audio(16000, 12, clicks=[1, 9], silent=[(4, 7)], channels=2)
    c2 = track_audio(16000, 4, clicks=[2], channels=2)
    tracks = [
        dict(id="a", kind="microphone", spans=[
            dict(file="track-a-1.wav", sampleRate=RATE, channels=1, frames=len(a1), startOffsetMs=0, gapFrames=4 * RATE, gapMaterialized=True),
            dict(file="track-a-2.wav", sampleRate=RATE, channels=1, frames=len(a2), startOffsetMs=20000),
        ], write={"track-a-1.wav": (a1, RATE, 1), "track-a-2.wav": (a2, RATE, 1)}),
        dict(id="b", kind="remote", spans=[
            dict(file="track-b-1.wav", sampleRate=RATE, channels=1, frames=len(b1), startOffsetMs=3600000),
            dict(file="track-b-2.wav", sampleRate=RATE, channels=1, frames=len(b2), startOffsetMs=3610000),
        ], write={"track-b-1.wav": (b1, RATE, 1), "track-b-2.wav": (b2, RATE, 1)}),
        dict(id="c", kind="tab", spans=[
            dict(file="track-c-1.wav", sampleRate=16000, channels=2, frames=len(c1), startOffsetMs=300000, gapFrames=3 * 16000),
            dict(file="track-c-2.wav", sampleRate=16000, channels=2, frames=len(c2), startOffsetMs=312000),
        ], write={"track-c-1.wav": (c1, 16000, 2), "track-c-2.wav": (c2, 16000, 2)}),
    ]  # fmt: skip
    sid, _directory = new_session(tmp_path, tracks)
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    path = tmp_path / result.name
    facts = ogg.validate_path(path)
    total = 3615 * RATE
    assert abs(facts.playable - total) <= FRAME and abs(published_samples(path) - total) <= FRAME
    decoder = decoder_for(path, tmp_path)
    try:
        markers = [2, 8, 16, 25, 3601, 3612, 301, 309, 314]
        for seconds in markers:
            found = marker_at(decoder, seconds * RATE)
            assert abs(found - seconds * RATE) <= FRAME, (seconds, found - seconds * RATE)
        tone = rms(decoder, 4 * RATE, RATE // 2)
        assert tone > 0.005
        for start, end in [(10, 14), (3606, 3610), (304, 307), (30, 300), (316, 3600)]:
            # Gaps are silent: neither inserted twice (markers above would move) nor filled.
            assert rms(decoder, int((start + 0.1) * RATE), int((min(end, start + 5) - start - 0.2) * RATE)) < tone / 20
    finally:
        decoder.close()


LONG_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path[:0] = [sys.argv[1], sys.argv[2]]
    from cassini_viewer.library_v2 import finalize
    result = finalize.finalize(sys.argv[3], sys.argv[4], zone="UTC")
    print(result.name)
    """
)


DRIFTS = [100e-6, -100e-6, 60e-6, -40e-6]  # each source clock, fast or slow
STARTS = [0, 7, 13, 19]  # seconds


def drifting_meeting(root, seconds, rotate_at):
    """4 sources with drifting clocks, anchors every minute, click markers; source 0 rotates."""
    tracks, expected = [], []
    for k, (ppm, start) in enumerate(zip(DRIFTS, STARTS, strict=True)):
        rate_true = RATE * (1 + ppm)  # samples this source writes per true second
        length = seconds - start
        marks = [3 + k * 2, length / 2 + k * 2, length - 8 + k * 2]
        expected += [start + m for m in marks]
        cuts = [(0, length)] if k else [(0, rotate_at), (rotate_at, length)]
        spans = []
        for first, last in cuts:
            frames = round((last - first) * rate_true)
            base = round(first * rate_true)
            burst = click(RATE)

            def block(offset, count, base=base, marks=marks, rate_true=rate_true, burst=burst):
                out = np.zeros(count)
                for m in marks:
                    at = round(m * rate_true) - base - len(burst) // 2 - offset
                    lo, hi = max(at, 0), min(at + len(burst), count)
                    if lo < hi:
                        out[lo:hi] = burst[lo - at : hi - at]
                return out

            name = f"track-s{k}-{len(spans)}.wav"
            write_wav_blocks(root / name, frames, RATE, block)
            anchors = [[round(t * rate_true) - base, int((t - first) * 10**9)] for t in range(first, last + 1, 60)]
            spans.append(dict(file=name, sampleRate=RATE, channels=1, frames=frames,
                              startOffsetMs=(start + first) * 1000, clockAnchors=anchors))  # fmt: skip
        tracks.append(dict(id=f"s{k}", kind="remote", spans=spans))
    sid, directory = new_session(root, tracks)
    for wav in root.glob("track-*.wav"):
        os.replace(wav, directory / wav.name)
    return sid, expected


def finalize_in_child(root, sid):
    """(published name, peak RSS in bytes) of finalize() run in a fresh process."""
    src = str(Path(__file__).parents[1] / "src")
    child = subprocess.Popen(
        [sys.executable, "-c", LONG_CHILD, src, str(Path(__file__).parent), str(root), sid], stdout=subprocess.PIPE
    )
    output = child.stdout.read().decode().strip()
    _pid, status, usage = os.wait4(child.pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return output, usage.ru_maxrss * 1024


def test_long_meeting_is_bounded_and_drift_anchored(tmp_path):
    """§9.4 scaled for CI: 10 min x 4 sources instead of 10 h (60x shorter), drift kept at +-100 ppm.

    Each source clock runs fast or slow by up to 100 ppm, so by the end its nominal sample count
    is off by up to 60 ms (3x the 20 ms tolerance); anchors every minute put the end markers back
    within one Opus frame. Source 0 rotates to a second file at 5 min.

    Memory: the same finalize of a 1 min meeting gives the platform's baseline (interpreter, NumPy,
    GStreamer; meson test also sets MALLOC_PERTURB_). 10x more audio may add at most 32 MiB to that
    peak, while one float32 full-timeline buffer for one source alone would add 115 MB (6.9 GB at 10 h).
    """
    seconds = 10 * 60
    short = tmp_path / "short"
    short.mkdir()
    baseline = finalize_in_child(short, drifting_meeting(short, 60, 30)[0])[1]
    shutil.rmtree(short)
    sid, expected = drifting_meeting(tmp_path, seconds, 300)
    output, peak = finalize_in_child(tmp_path, sid)
    assert peak - baseline < 32 << 20 < seconds * RATE * 4, (baseline, peak)
    # Without the anchors the end markers would be off by up to this much, far beyond one frame.
    assert max(abs(d) for d in DRIFTS) * seconds * RATE > 2.5 * FRAME
    path = tmp_path / output
    facts = ogg.validate_path(path)
    assert abs(facts.playable - seconds * RATE) <= FRAME
    decoder = decoder_for(path, tmp_path)
    try:
        for at in expected:
            found = marker_at(decoder, round(at * RATE))
            assert abs(found - at * RATE) <= FRAME, (at, found - at * RATE)
    finally:
        decoder.close()


def test_limiter_fast_path_matches_while_the_gain_recovers():
    """Blocks with no peak above 0.95 skip the peak search; the gain must still recover as export's does."""
    t = np.arange(40000)
    signal = 0.3 * np.sin(t / 7)
    signal[9000] = 3.0  # one loud block, then quiet blocks while the gain recovers over 50 ms release
    expected = np.frombuffer(b"".join(a.tobytes() for a in limited_pcm([array.array("f", signal)])), dtype=np.float32)
    blocks = [signal[i : i + 4800].astype(np.float32).astype(np.float64) for i in range(0, 40000, 4800)]
    got = np.concatenate(list(mixdown.limited(blocks)))
    np.testing.assert_allclose(got.astype(np.float32), expected, atol=2e-6)
    assert abs(got[9100]) < 0.5 * abs(signal[9100])  # still attenuated 100 samples after the peak
