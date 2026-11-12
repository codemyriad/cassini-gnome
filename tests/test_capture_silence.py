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

The exact counts are what the same conversion chain makes of the same input,
run on its own (converted_frames), so they hold on each distribution's
GStreamer. The broker drops a side's PCM until that side commits (R79 §1), and
its commit writes durable checkpoints: its stand-in sends its BUFFERS only once
the side has committed, so no fsync latency decides how much is recorded.

The legacy Recorder (recorder.Recorder, Ogg Opus) is not covered here: no live
user path reaches it. test_capture_convert still runs its capture_pipeline
conversion chain.
"""

import array
import logging
import math
import re
import threading
import time
import wave
from fractions import Fraction
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
RATES = (44100, 48000)
# Bounds only (E1): each wait ends on its event.
BOUND_S = 30
# The pulsesrc element of a capture description, up to its first link.
PULSESRC = re.compile(r"pulsesrc [^!]*")


def silence_source(rate, buffers=BUFFERS, *, live=True):
    """Float digital silence in 10 ms buffers, without GAP flags; endless
    when `buffers` is None (Feed then ends it).

    audiotestsrc's wave=silence (and volume=0) mark their buffers GAP, which
    audioconvert passes through untouched, so they never meet the dither. A
    sine at 0 Hz is sin(0) = +0.0 in every sample, delivered as real data, as
    pulsesrc delivers a silent device. Feed checks every buffer it sends.
    """
    count = "" if buffers is None else f" num-buffers={buffers}"
    return (
        f"audiotestsrc name=capture-source wave=sine freq=0 is-live={str(live).lower()}{count} "
        f"samplesperbuffer={rate // 100} ! audio/x-raw,format=F32LE,layout=interleaved,channels=2,rate={rate}"
    )


def converted_frames(rate, buffers):
    """The 48 kHz frames the capture conversion makes of `buffers` stand-in
    buffers, counted on its own through to EOS.

    At 48 kHz audioresample passes through: buffers * 480. At 44.1 kHz it holds
    back its filter latency and its drain at EOS pushes out what it held; on
    GStreamer 1.28 that is 72000 - 35 + 34 = 71999 frames for 150 buffers. The
    chain alone sets the exact count, never timing, and the recorder must save
    exactly that, so the reference is this run, not a number measured on one
    GStreamer build.
    """
    pipeline = Gst.parse_launch(
        silence_source(rate, buffers, live=False) + " ! queue ! " + CAPTURE_CONVERT + " ! audioresample ! "
        "audio/x-raw,format=S16LE,rate=48000,channels=1 ! appsink name=pcm sync=false"
    )
    sink = pipeline.get_by_name("pcm")
    frames = 0
    pipeline.set_state(Gst.State.PLAYING)
    try:
        while (sample := sink.emit("try-pull-sample", BOUND_S * Gst.SECOND)) is not None:
            frames += sample.get_buffer().get_size() // 2
        assert sink.get_property("eos"), "the reference conversion did not reach EOS"
        message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING)
        assert message is None, message and message.get_structure().to_string()
    finally:
        pipeline.set_state(Gst.State.NULL)
    plain = buffers * (rate // 100) * 48000 // rate
    if rate == 48000:
        assert frames == plain
    else:
        # A sanity bound, not a tolerance on the recorder: under 1 ms off the ratio.
        assert abs(frames - plain) < 48, (frames, plain)
    return frames


class Feed:
    """Swap only the pulsesrc element of each tap's own description.

    Notes the descriptions parsed, what each stand-in sent (float samples,
    non-zero bytes, GAP buffers), and the bus errors and warnings of the
    pipelines parsed whole (NativeTap); NativeRecorder's bus is watched by the
    test itself.

    Without `committed`, each stand-in sends BUFFERS buffers and ends. With it
    (kind -> Event, set as that side commits), a stand-in sends readiness
    buffers until its side has committed, then exactly BUFFERS more, then EOS
    (sent to the source, as pulsesrc ends). A buffer let through after the
    commit started reaches the tap's append after the commit, never dropped as
    readiness PCM, however long its checkpoints take.
    """

    def __init__(self, monkeypatch, rate, committed=None):
        self.rate, self.committed = rate, committed
        self.parsed, self.problems, self.sent, self.enders = [], [], [], []
        for name in ("parse_launch", "parse_bin_from_description"):
            monkeypatch.setattr(Gst, name, self._parser(getattr(Gst, name)))

    def _parser(self, original):
        def parse(description, *args):
            if "pulsesrc" not in description:
                return original(description, *args)
            kind = "microphone" if "device=@DEFAULT_SOURCE@" in description else "system"
            buffers = None if self.committed else BUFFERS
            # Finite feeds need the same PCM and EOS without wall-clock pacing.
            # Broker feeds stay live to align their PTS with the durable commit.
            replacement = silence_source(self.rate, buffers, live=self.committed is not None)
            description, count = PULSESRC.subn(replacement + " ", description, count=1)
            assert count == 1 and CAPTURE_CONVERT in description
            self.parsed.append(description)
            element = original(description, *args)
            sent = dict(kind=kind, samples=0, nonzero=0, gaps=0, readiness=0, after=0)
            self.sent.append(sent)
            source = element.get_by_name("capture-source")
            source.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._check, sent, source)
            if isinstance(element, Gst.Pipeline):
                bus = element.get_bus()
                bus.enable_sync_message_emission()
                bus.connect("sync-message", lambda _bus, message: self.watch(message))
            return element

        return parse

    def _check(self, _pad, info, sent, source):
        if self.committed is not None:
            if not self.committed[sent["kind"]].is_set():
                sent["readiness"] += 1
            elif sent["after"] == BUFFERS:
                return Gst.PadProbeReturn.DROP  # Sent between the last buffer and the EOS.
            else:
                sent["after"] += 1
                if sent["after"] == BUFFERS:
                    # Not from the streaming thread: basesrc pushes it next.
                    ender = threading.Thread(target=source.send_event, args=(Gst.Event.new_eos(),))
                    self.enders.append(ender)
                    ender.start()
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
        """Both sides' stand-ins sent all of their float zeros, as real data.
        Returns how many buffers each side's stand-in sent, by kind."""
        assert len(self.parsed) == 2 and not self.problems, self.problems
        for ender in self.enders:
            ender.join(BOUND_S)
            assert not ender.is_alive()
        buffers = {}
        for sent in self.sent:
            if self.committed is None:
                assert sent["readiness"] == sent["after"] == 0
                buffers[sent["kind"]] = BUFFERS
            else:
                assert sent["after"] == BUFFERS, sent
                buffers[sent["kind"]] = sent["readiness"] + BUFFERS
            assert sent["samples"] == buffers[sent["kind"]] * self.rate // 100 * 2, sent
            assert sent["nonzero"] == sent["gaps"] == 0, sent
        assert sorted(buffers) == ["microphone", "system"]
        return buffers


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


@pytest.mark.parametrize("rate", RATES)
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
        assert feed.check() == dict(microphone=BUFFERS, system=BUFFERS)
        expected = converted_frames(rate, BUFFERS)
        assert recorder.session.manifest["state"] == "completed"
        assert recorder.session.manifest["events"][-1]["type"] == "end"
        tracks = saved_tracks(root)
        assert sorted(tracks) == ["microphone", "system"]
        for kind, track in tracks.items():
            frames, nonzero = saved_pcm(track)
            assert nonzero == 0, f"{kind}: {nonzero} of {frames} saved samples are not zero ({rate} Hz input)"
            assert frames == track.frames == expected, kind
            assert silent(track), kind
    finally:
        if recorder.pipeline is not None and not recorder.stopping:
            recorder.stop()
        run_until(lambda: recorder.pipeline is None and not recorder.stopping)
        recorder.close()


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("signal_owned", [False, True])
def test_broker_native_taps_save_float_silence_as_zeros(tmp_path, monkeypatch, caplog, rate, signal_owned):
    """browser_capture's NativeTap sides (Me, and computer sound with no browser
    audio), stopped by BrowserCapture.stop after their sources ended."""
    root = library(tmp_path, monkeypatch)
    committed = dict(microphone=threading.Event(), system=threading.Event())
    feed = Feed(monkeypatch, rate, committed)
    add_native = BrowserCapture._add_native

    def committing(self, kind, *args, **kwargs):
        # The commit adds the side's track under capture.lock, before its
        # checkpoint; the tap's append waits on that lock, so every buffer the
        # stand-in sends from here is appended to the committed side.
        identifier = add_native(self, kind, *args, **kwargs)
        committed[kind].set()
        return identifier

    monkeypatch.setattr(BrowserCapture, "_add_native", committing)
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
    capture.signal_owned = signal_owned
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
            reason="start" if signal_owned else "missing",
            others_route=dict(source="system", reason="mixedSiteSignal", site="Synthetic meeting")
            if signal_owned
            else None,
            # Native Me: signal-owned captures, and companions before sender-me-v1.
            native_me=True,
        )
        assert sorted(capture.native_taps) == ["microphone", "system"]
        for tap in taps.values():
            assert tap.ended.wait(BOUND_S), f"{tap.kind} tap did not reach EOS"
        assert capture.stop()["type"] == "stopped"
        assert capture.stopped.wait(BOUND_S) and capture.publication is not None
        assert not capture.error and not failures and not logged_warnings(caplog)
        buffers = feed.check()
        if signal_owned:
            from cassini_talk.capture_details import read_details
            from cassini_talk.strings import text

            (meeting,) = LibraryScanner(root).scan().meetings
            assert text("details.othersRoute.mixedGeneric") in read_details(meeting).notes
        tracks = saved_tracks(root)
        assert sorted(tracks) == ["microphone", "system"]
        for kind, track in tracks.items():
            sizes = [size for size, _, _ in calls[kind]]
            saved = [0] + [frames for _, _, frames in calls[kind]]
            written = [after - before for before, after in pairwise(saved)]
            # The tap delivered every frame of the input: its readiness
            # buffers and the BUFFERS sent after the commit.
            assert sum(sizes) == converted_frames(rate, buffers[kind]), kind
            # R79 §1: PCM before the side commits is readiness PCM and is not
            # recorded (at least the first buffer; how many is commit timing).
            first = next(index for index, value in enumerate(written) if value)
            assert first >= 1 and not any(written[:first])
            # The track starts at its commit time, inside a buffer: the first
            # recorded buffer loses its frames before that time (selected_pcm),
            # or follows it after a zero prefix (append_native) that
            # session.json records (D52.2). Count on an exact rational clock:
            # native ns plus whole 48 kHz frames share 1/3-ns ticks.
            offset, start = calls[kind][first][1], track.start_offset_ms
            offset_q = Fraction(round(offset * 3_000_000), 3_000_000)
            start_q = Fraction(round(start * 3_000_000), 3_000_000)
            trim = max(0, math.ceil((start_q - offset_q) * 48))
            prefix = max(0, round((offset_q + Fraction(trim, 48) - start_q) * 48))
            assert written[first] == sizes[first] - trim + prefix, kind
            gaps = [(gap["kind"], gap["frames"]) for gap in track.gaps or ()]
            assert gaps == ([("prefix", prefix)] if prefix else []), kind
            # Every later buffer is saved whole: no truncation, padding or gap.
            assert written[first + 1 :] == sizes[first + 1 :], kind
            # Enough converted silence to flip D56 if any of it were dithered:
            # all BUFFERS sent after the commit are recorded.
            assert sum(sizes[first + 1 :]) >= MIN_FRAMES * frame_samples(48000)
            frames, nonzero = saved_pcm(track)
            assert nonzero == 0, f"{kind}: {nonzero} of {frames} saved samples are not zero ({rate} Hz input)"
            assert frames == track.frames == prefix + sum(sizes[first:]) - trim, kind
            assert silent(track), kind
    finally:
        if not capture.finished:
            capture.stop(reason="appQuit")
            assert capture.stopped.wait(BOUND_S)
