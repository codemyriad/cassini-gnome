# SPDX-License-Identifier: GPL-3.0-or-later
"""D56 end to end: float digital silence is saved as all-zero PCM.

silent_tracks counts a 32 ms frame as sound when any PCM16 sample is != 0, so a
silent device must reach the saved track as exact zeros. Each test records
through the shipped capture code, with only the pulsesrc element of each tap's
own description replaced by a finite source of float (F32LE, stereo) zeros at
48 or 44.1 kHz. Everything after that element is production code: the tap's
conversion chain, its appsink callback, the session's WAV writing, the source
EOS, the stop, finalization and publication into a Library. The saved tracks
are then read back as the Library scanner gives them to D56: every sample must
be 0, the frame count is exact, and D56 itself calls each track silent. The
stand-ins' own buffers are checked to be float zeros sent as data.

The legacy Recorder (recorder.Recorder, Ogg Opus) is not covered here: only
recording_window.py uses it, and that window is not installed (see
test_installed_modules.NOT_INSTALLED). test_capture_convert still runs its
capture_pipeline conversion chain.
"""

import array
import logging
import math
import re
import time
import wave
from itertools import pairwise

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("GStreamer typelib is unavailable", allow_module_level=True)
from gi.repository import GLib, Gst  # noqa: E402

from cassini_talk.browser_capture import BrowserCapture  # noqa: E402
from cassini_talk.library import LibraryScanner  # noqa: E402
from cassini_talk.native_recorder import NativeRecorder  # noqa: E402
from cassini_talk.native_tap import NativeTap  # noqa: E402
from cassini_talk.recorder import CAPTURE_CONVERT  # noqa: E402
from cassini_talk.silent_tracks import MIN_FRAMES, frame_samples, silent  # noqa: E402

Gst.init(None)

# 1.5 s of input in 10 ms device buffers: 46 whole D56 frames at 48 kHz, more
# than the 32 that make a track count as sound, so dither would flip D56.
BUFFERS = 150
# Saved frames at 48 kHz for 1.5 s of input. At 48 kHz audioresample passes
# through: 150 * 480. At 44.1 kHz the plain ratio is 66150 * 48000 / 44100 =
# 72000; audioresample (default quality) holds back its filter latency, 35
# output frames (its first buffer is 445 frames, not 480), and its drain at EOS
# pushes out 34: 72000 - 35 + 34 = 71999. That depends only on the input and
# its buffer layout, never on timing (the same -1 at 100, 150 and 151 buffers).
EXPECTED = {48000: 72000, 44100: 71999}
# Bounds only (E1): each wait ends on its event.
BOUND_S = 30
# The pulsesrc element of a capture description, up to its first link.
PULSESRC = re.compile(r"pulsesrc [^!]*")


def silence_source(rate):
    """Live, finite float digital silence, without GAP flags.

    audiotestsrc's wave=silence (and volume=0) mark their buffers GAP, which
    audioconvert passes through untouched, so they never meet the dither. A
    sine at 0 Hz is sin(0) = +0.0 in every sample, delivered as real data, as
    pulsesrc delivers a silent device. Feed checks every buffer it sends.
    """
    return (
        f"audiotestsrc name=capture-source wave=sine freq=0 is-live=true num-buffers={BUFFERS} "
        f"samplesperbuffer={rate // 100} ! audio/x-raw,format=F32LE,layout=interleaved,channels=2,rate={rate}"
    )


class Feed:
    """Swap only the pulsesrc element of each tap's own description.

    Notes the descriptions parsed, what each stand-in sent (float samples,
    non-zero bytes, GAP buffers), and the bus errors and warnings of the
    pipelines parsed whole (NativeTap); NativeRecorder's bus is watched by the
    test itself.
    """

    def __init__(self, monkeypatch, rate):
        self.rate = rate
        self.parsed, self.problems, self.sent = [], [], []
        for name in ("parse_launch", "parse_bin_from_description"):
            monkeypatch.setattr(Gst, name, self._parser(getattr(Gst, name)))

    def _parser(self, original):
        def parse(description, *args):
            if "pulsesrc" not in description:
                return original(description, *args)
            description, count = PULSESRC.subn(silence_source(self.rate) + " ", description, count=1)
            assert count == 1 and CAPTURE_CONVERT in description
            self.parsed.append(description)
            element = original(description, *args)
            sent = dict(samples=0, nonzero=0, gaps=0)
            self.sent.append(sent)
            pad = element.get_by_name("capture-source").get_static_pad("src")
            pad.add_probe(Gst.PadProbeType.BUFFER, self._check, sent)
            if isinstance(element, Gst.Pipeline):
                bus = element.get_bus()
                bus.enable_sync_message_emission()
                bus.connect("sync-message", lambda _bus, message: self.watch(message))
            return element

        return parse

    @staticmethod
    def _check(_pad, info, sent):
        buffer = info.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        sent["samples"] += len(data) // 4
        sent["nonzero"] += len(data) - data.count(0)
        sent["gaps"] += bool(buffer.get_flags() & Gst.BufferFlags.GAP)
        return Gst.PadProbeReturn.OK

    def watch(self, message):
        if message.type in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
            self.problems.append(f"{message.src.get_name()}: {message.get_structure()}")

    def check(self):
        """Both sides' stand-ins sent all of their float zeros, as real data."""
        assert len(self.parsed) == 2 and not self.problems, self.problems
        whole = dict(samples=BUFFERS * self.rate // 100 * 2, nonzero=0, gaps=0)
        assert self.sent == [whole, whole]


def run_until(predicate):
    """Dispatch GLib events until `predicate` holds, BOUND_S at most (E1)."""
    expired = []
    bound = GLib.timeout_add_seconds(BOUND_S, lambda: expired.append(True))
    context = GLib.MainContext.default()
    while not predicate() and not expired:
        context.iteration(True)
    if not expired:
        GLib.source_remove(bound)
    assert predicate(), "timed out"


def library(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return root


def saved_tracks(root):
    """The published meeting's capture tracks, as the scanner hands them to D56."""
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.metadata["published"]
    return {track.kind: track for track in meeting.tracks}


def saved_pcm(track):
    """(frames, non-zero samples) over every file the track's spans name."""
    frames = nonzero = 0
    for span in track.spans or (track,):
        assert span.codec == "pcm"
        with wave.open(str(span.path), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (48000, 1, 2)
            data = audio.readframes(audio.getnframes())
        assert len(data) == audio.getnframes() * 2
        frames += audio.getnframes()
        nonzero += len(data) // 2 - array.array("h", data).count(0)
    return frames, nonzero


def logged_warnings(caplog):
    """What the capture code logged at WARNING or above (each such line is a fault here)."""
    records = (r for r in caplog.records if r.name.startswith("cassini_talk"))
    return [r.getMessage() for r in records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("rate", sorted(EXPECTED))
def test_native_recording_of_float_silence_saves_only_zeros(tmp_path, monkeypatch, caplog, rate):
    """main.py's NativeRecorder: both sides, ended by their sources' EOS."""
    root = library(tmp_path, monkeypatch)
    feed = Feed(monkeypatch, rate)
    message = NativeRecorder._message

    def watched(self, bus, value):
        feed.watch(value)
        return message(self, bus, value)

    monkeypatch.setattr(NativeRecorder, "_message", watched)
    caplog.set_level(logging.WARNING)
    recorder = NativeRecorder()
    errors, finished = [], []
    recorder.connect("error", lambda _recorder, value: errors.append(value))
    recorder.connect("finished", lambda _recorder, path: finished.append(path))
    try:
        # Not test=True (tone sources): the shipped pulsesrc descriptions. No
        # output-mute observer: it needs a sound server and writes no PCM.
        recorder.start(observe_output=False)
        # The sources' EOS ends the recording: _sample, the bus EOS,
        # _begin_finish, session.finish and the Library publication.
        run_until(lambda: finished)
        assert finished[0] and not errors and not logged_warnings(caplog)
        feed.check()
        assert recorder.session.manifest["state"] == "completed"
        assert recorder.session.manifest["events"][-1]["type"] == "end"
        tracks = saved_tracks(root)
        assert sorted(tracks) == ["microphone", "system"]
        for kind, track in tracks.items():
            frames, nonzero = saved_pcm(track)
            assert nonzero == 0, f"{kind}: {nonzero} of {frames} saved samples are not zero ({rate} Hz input)"
            assert frames == track.frames == EXPECTED[rate], kind
            assert silent(track), kind
    finally:
        if recorder.pipeline is not None and not recorder.stopping:
            recorder.stop()
        run_until(lambda: recorder.pipeline is None and not recorder.stopping)
        recorder.close()


@pytest.mark.parametrize("rate", sorted(EXPECTED))
def test_broker_native_taps_save_float_silence_as_zeros(tmp_path, monkeypatch, caplog, rate):
    """browser_capture's NativeTap sides (Me, and computer sound with no browser
    audio), stopped by BrowserCapture.stop after their sources ended."""
    root = library(tmp_path, monkeypatch)
    feed = Feed(monkeypatch, rate)
    caplog.set_level(logging.WARNING)
    taps, calls, failures = {}, {}, []
    capture = None

    def factory(kind, origin_ms, append, failed, *, level):
        # The real tap and the real append; this only notes each buffer's
        # length and offset, and the track's saved frames once it returned.
        log = calls.setdefault(kind, [])

        def counted(data, offset, *, host_ns=None):
            append(data, offset, host_ns=host_ns)
            with capture.lock:
                tracks = capture.host.session.manifest["tracks"]
                saved = next((t["frames"] for t in tracks if t["kind"] == kind), 0)
                log.append((len(data) // 2, offset, saved))

        def fail(value):
            failures.append(value)
            failed(value)

        taps[kind] = NativeTap(kind, origin_ms, counted, fail, level=level)
        return taps[kind]

    capture = BrowserCapture(lambda *_: None, tap_factory=factory)
    try:
        capture.start(
            dict(
                type="start",
                version=1,
                independent=True,
                captureId=f"silence-{rate}",
                timelineOriginMs=int(time.time() * 1000),
                microphone=True,
                title="Silence",
            ),
            others_source="system",
            reason="missing",
        )
        assert sorted(capture.native_taps) == ["microphone", "system"]
        for tap in taps.values():
            assert tap.ended.wait(BOUND_S), f"{tap.kind} tap did not reach EOS"
        assert capture.stop()["type"] == "stopped"
        assert capture.stopped.wait(BOUND_S) and capture.publication is not None
        assert not capture.error and not failures and not logged_warnings(caplog)
        feed.check()
        tracks = saved_tracks(root)
        assert sorted(tracks) == ["microphone", "system"]
        for kind, track in tracks.items():
            sizes = [size for size, _, _ in calls[kind]]
            saved = [0] + [frames for _, _, frames in calls[kind]]
            written = [after - before for before, after in pairwise(saved)]
            # The tap delivered every frame of the input.
            assert sum(sizes) == EXPECTED[rate], kind
            # R79 §1: PCM before the side commits is readiness PCM and is not
            # recorded (at least the first buffer; how many is commit timing).
            first = next(index for index, value in enumerate(written) if value)
            assert first >= 1 and not any(written[:first])
            # The track starts at its commit time, inside a buffer: the first
            # recorded buffer loses its frames before that time (selected_pcm),
            # or follows it after a zero prefix (append_native) that
            # session.json records (D52.2). The same arithmetic, written out:
            offset, start = calls[kind][first][1], track.start_offset_ms
            trim = max(0, math.ceil((start - offset) * 48000 / 1000))
            prefix = max(0, round((offset + trim * 1000 / 48000 - start) * 48000 / 1000))
            assert written[first] == sizes[first] - trim + prefix, kind
            gaps = [(gap["kind"], gap["frames"]) for gap in track.gaps or ()]
            assert gaps == ([("prefix", prefix)] if prefix else []), kind
            # Every later buffer is saved whole: no truncation, padding or gap.
            assert written[first + 1 :] == sizes[first + 1 :], kind
            # Enough converted silence to flip D56 if any of it were dithered.
            assert sum(sizes[first + 1 :]) >= MIN_FRAMES * frame_samples(48000)
            frames, nonzero = saved_pcm(track)
            assert nonzero == 0, f"{kind}: {nonzero} of {frames} saved samples are not zero ({rate} Hz input)"
            assert frames == track.frames == prefix + sum(sizes[first:]) - trim, kind
            assert silent(track), kind
    finally:
        if not capture.finished:
            capture.stop(reason="appQuit")
            assert capture.stopped.wait(BOUND_S)
