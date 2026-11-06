# SPDX-License-Identifier: GPL-3.0-or-later
"""D69/D4: real appsrc queues and real WAVs, gated at the writer, without sleeps."""

import json
import threading
import wave

import pytest

from cassini_talk.native_queue import NativeQueue
from cassini_talk.native_timing import NativeTimeline
from cassini_talk.recording_session import RecordingSession

BOUND = 10


def test_exact_admission_is_bounded_and_loss_metadata_does_not_grow_with_stall():
    queue = NativeQueue(8000, capacity_frames=80)
    assert queue.enter(0, 80, 0)
    assert queue.take(0) == (80, 0)  # In flight still occupies the budget.
    for n in range(10000):
        assert not queue.enter(n + 1, 80, n + 1)
    assert len(queue._pending) == 0
    queue.done(80)
    assert queue.enter(10001, 80, 10001)
    assert queue.take(10001) == (80, 800000)
    queue.done(80)
    queue.seal(10002)
    queue.seal(10003)  # A later Stop cannot extend a deselected source's cut.
    assert not queue.enter(10002, 80, 10002)
    assert queue.tail() == 0  # Post-Stop callbacks are not lost audio.


@pytest.mark.parametrize(
    "path,survivor,at_stop",
    [
        ("tap", False, False),
        ("tap", True, False),
        ("recorder", False, False),
        ("recorder", True, False),
        ("recorder", True, True),
    ],
)
def test_real_native_queue_preserves_mac_callback_repro_in_wav(tmp_path, monkeypatch, path, survivor, at_stop):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    from cassini_talk.native_recorder import NativeRecorder
    from cassini_talk.native_tap import NativeTap
    from cassini_talk.recorder import TAP_QUEUE

    Gst.init(None)
    session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    entered, release, dropped, written, following = (threading.Event() for _ in range(5))
    original = session.writers["microphone"].append
    first = True

    def gated(data):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(BOUND)
        original(data)
        written.set()

    monkeypatch.setattr(session.writers["microphone"], "append", gated)
    recorder = None
    timeline = NativeTimeline(48000)
    if path == "recorder":
        recorder = NativeRecorder()
        recorder.session = session
        recorder._test_sources = True
        pipeline = recorder.pipeline = Gst.Pipeline.new(None)
        parse = Gst.parse_bin_from_description

        def appsrc(description, *args):
            # Replace only the device; retain production convert/admission/queue/appsink.
            return parse("appsrc name=source format=time ! " + description.split(" ! ", 1)[1], *args)

        monkeypatch.setattr(Gst, "parse_bin_from_description", appsrc)
        branch = recorder._branch("microphone")
        pipeline.add(branch)
        recorder._branches["microphone"] = branch
        admission = branch._writer_queue
        admission.capacity = 480
        recorder._timelines["microphone"] = timeline
        source = branch.get_by_name("source")
    else:

        def append(data, offset, *, host_ns=None, writer_frames=0):
            mapped, gap = timeline.map(round(offset * Gst.MSECOND), len(data) // 2, offset, writer_frames=writer_frames)
            session.append_native("microphone", data, mapped, gap_frames=gap, writer_frames=timeline.writer_frames)

        tap = NativeTap("microphone", None, append, pytest.fail)
        pipeline = tap.pipeline = Gst.parse_launch(
            'appsrc name=source format=time caps="audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved" ! '
            + TAP_QUEUE
            + " ! appsink name=pcm emit-signals=true sync=false async=false"
        )
        tap.anchor_session = 0
        admission = tap.writer_queue
        admission.capacity = 480
        pipeline.get_by_name("tap-queue").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, tap._entered)
        pipeline.get_by_name("pcm").connect("new-sample", tap._sample)
        source = pipeline.get_by_name("source")
    source.set_property(
        "caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved")
    )
    admit = admission.enter

    def observed(stamp, frames, offset):
        accepted = admit(stamp, frames, offset)
        if stamp == 9 * Gst.MSECOND:
            assert not accepted
            dropped.set()
        if stamp == 19 * Gst.MSECOND:
            assert accepted
            following.set()
        return accepted

    monkeypatch.setattr(admission, "enter", observed)

    def push(at):
        pcm = b"\x01\x00" * 480
        buffer = Gst.Buffer.new_wrapped(pcm)
        buffer.pts = at * Gst.MSECOND
        buffer.duration = 10 * Gst.MSECOND
        assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK

    try:
        pipeline.set_state(Gst.State.PLAYING)
        if path == "tap":
            tap.anchor_clock = pipeline.get_base_time()
        push(0)
        assert entered.wait(BOUND)
        push(9)
        assert dropped.wait(BOUND)
        if not at_stop:
            admission.seal(29 if survivor else 19)
        release.set()
        assert written.wait(BOUND)
        # Wait for writer admission to free its budget, not just append's return.
        # EOS through the queue is the deterministic completion barrier.
        if survivor:
            # Gate a source-pad buffer after the first appsink callback returns.
            # A serialized downstream event traverses that same streaming thread.
            barrier = threading.Event()
            queue = pipeline.get_by_name("tap-queue") if path == "tap" else branch.get_by_name("tap-queue")
            queue.get_static_pad("src").add_probe(
                Gst.PadProbeType.IDLE, lambda *_: (barrier.set(), Gst.PadProbeReturn.REMOVE)[1]
            )
            assert barrier.wait(BOUND)
            if at_stop:
                with recorder._lock:
                    push(19)
                    assert following.wait(BOUND)
                    # The follower is admitted before Stop, but cannot acquire
                    # the writer lock until its PCM is outside the boundary.
                    recorder._stop_at_ms = 19
                    admission.seal(19)
            else:
                push(19)
        assert source.emit("end-of-stream") == Gst.FlowReturn.OK
        message = pipeline.get_bus().timed_pop_filtered(BOUND * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert message is not None and message.type == Gst.MessageType.EOS
        pipeline.set_state(Gst.State.NULL)
        stop = 29 if survivor and not at_stop else 19
        if recorder is not None:
            # Exercise the production finalizer, including flushing its exact
            # queue tail before the session's durable end checkpoint.
            loop, done = GLib.MainLoop(), []
            recorder._stop_at_ms = stop
            recorder.end_reason = "user"
            recorder.connect("finished", lambda *_: (done.append(True), loop.quit()))
            timer = GLib.timeout_add_seconds(BOUND, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
            recorder._begin_finish()
            loop.run()
            if done:
                GLib.source_remove(timer)
            assert done, "Native finalization never emitted finished"
        else:
            timeline.flush_loss(session, "microphone", admission, stop)
            session.finish(reason="user", at_ms=stop)
        saved = json.loads((session.directory / "session.json").read_text())
        track = saved["tracks"][0]
        assert (saved["state"], saved["problemKind"], saved["events"][-1]["reason"]) == ("interrupted", "io", "error")
        assert track["frames"] == (1440 if survivor and not at_stop else 960)
        assert track["droppedFrames"] == 480 and not track.get("gapFrames", 0)
        assert track["gaps"] == [dict(atMs=10, durationMs=10, frames=480, kind="writer")]
        assert saved["events"][-1]["atMs"] == (30 if survivor and not at_stop else 20)
        assert (
            saved["problem"]
            == "10 ms of microphone audio was lost because the computer could not keep up; the gaps contain silence."
        )
        with wave.open(str(session.directory / track["file"])) as wav:
            data = wav.readframes(wav.getnframes())
        assert data[960:1920] == bytes(960)
        assert data[:960] == b"\x01\x00" * 480
        if survivor and not at_stop:
            assert data[1920:] == b"\x01\x00" * 480
    finally:
        release.set()
        pipeline.set_state(Gst.State.NULL)
        session.finish()
        if recorder is not None:
            recorder.pipeline = None
            recorder.close()


@pytest.mark.parametrize("stop,last,track_end,expected", [(19, 0, 20, 20), (25, 0, 20, 25), (19, 30, 20, 30)])
def test_d4_end_is_latest_of_stop_last_event_and_accepted_sample_end(tmp_path, stop, last, track_end, expected):
    session = RecordingSession(tmp_path)
    session.add_track("mic", "Me", "microphone", 8000, 1)
    session.append_native("mic", b"\x01\x00" * (track_end * 8), 0)
    session.manifest["events"] = [dict(type="selection", atMs=last, me=True, others=False, reason="start")]
    session.finish(reason="user", at_ms=stop)
    assert session.manifest["events"][-1] == dict(type="end", atMs=expected, reason="user")


@pytest.mark.parametrize("known", [0, 80])
def test_timeout_tail_infers_only_unobserved_host_time_after_known_frames(tmp_path, known):
    session = RecordingSession(tmp_path)
    session.add_track("mic", "Me", "microphone", 8000, 1)
    timeline, admission = NativeTimeline(8000), NativeQueue(8000, capacity_frames=80)
    assert admission.enter(0, 80, 0)
    admission.take(0)
    offset, gap = timeline.map(0, 80, 0)
    session.append_native("mic", b"\x01\x00" * 80, offset, gap_frames=gap)
    if known:
        assert not admission.enter(9_000_000, 80, 9)
    admission.done(80)
    admission.seal(25)
    assert not admission.enter(25_000_000, 80, 25)
    timeline.flush_loss(session, "mic", admission, 25, inferred=True)
    session.finish(reason="user", at_ms=25)
    track = session.manifest["tracks"][0]
    # Known 10 ms (sample time) + inferred 6 ms (host 19..25), or inferred 15 ms.
    assert track["droppedFrames"] == (128 if known else 120)
    assert track["frames"] == (208 if known else 200)
    assert session.manifest["events"][-1]["reason"] == "error"


@pytest.mark.parametrize("following_at_stop", [False, True, "just-before"])
def test_browser_capture_stop_flushes_known_tail_and_never_counts_post_stop_callbacks(tmp_path, following_at_stop):
    """`just-before`: the following callback starts a fraction of a frame before
    the cut (agy minor on #239): it keeps one frame and still delivers the known
    drops it carries, so none are lost."""
    from test_tap_stall import DrainingTaps, start_capture, stop_in_background, tone

    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="browser")
    tap = taps.taps["microphone"]
    identifier = capture.native_taps["microphone"][0]
    start = capture._tap_states[identifier]["at"]
    tap.append(tone(480), start)
    admission = tap.writer_queue = NativeQueue(capacity_frames=480)
    assert admission.enter(0, 480, start)
    admission.take(0)  # A stalled writer still occupies its bounded budget.
    assert not admission.enter(9_000_000, 480, start + 9)
    admission.done(480)
    # Host ns and timeline ms of the callback that follows the stall.
    follower = (18_995_000, start + 19 - 0.005) if following_at_stop == "just-before" else (19_000_000, start + 19)
    if following_at_stop:
        assert admission.enter(follower[0], 480, follower[1])
    clock.ms = start + 19
    stopper, result = stop_in_background(capture, tap)
    try:
        admission.seal(tap.stop_boundary())
        if following_at_stop:
            frames, lost = admission.take(follower[0])
            tap.append(tone(frames), follower[1], writer_frames=lost)
            admission.done(frames)
        assert not admission.enter(30_000_000, 480, start + 30)
        tap.drained.set()
        stopper.join(BOUND)
        assert not stopper.is_alive() and "error" not in result, result
        saved = json.loads((capture.directory / "session.json").read_text())
        track = next(t for t in saved["tracks"] if t["id"] == identifier)
        # A follower starting before the cut keeps at least its first frame
        # (pcm_before rounds up), and the known drops it carries count.
        assert track["frames"] == (961 if following_at_stop == "just-before" else 960)
        assert track["droppedFrames"] == 480
        assert (saved["problemKind"], saved["events"][-1]["reason"]) == ("io", "error")
        extra = 1000 / 48000 if following_at_stop == "just-before" else 0  # its kept frame
        assert saved["events"][-1]["atMs"] == pytest.approx(start + 20 + extra)
    finally:
        tap.drained.set()
        stopper.join(BOUND)
        capture.close()


def test_writer_loss_is_one_sentence_per_source_summed_across_its_spans(tmp_path):
    """D69: turning a native source off and on opens a new span (a fresh track id,
    no sourceTrackId); its writer loss is summed exactly per source and rounded
    half-up once, not once per span (Mac #154)."""
    session = RecordingSession(tmp_path)
    for kind in ("microphone", "system"):
        for span, start in ((kind, 0), (f"{kind}-second-span", 100)):
            session.add_track(span, "Me" if kind == "microphone" else "The others", kind, 8000, 1)
            session.append_native(span, b"\x01\x00" * 80, start)
            # 2 frames at 8 kHz: 0.25 ms in each span, 0.5 ms per source.
            session.pad_native(span, writer_frames=2)
            if start == 0:
                session.end_track(span)
    session.finish("The tab was closed.", reason="error")
    sentence = "1 ms of {} audio was lost because the computer could not keep up; the gaps contain silence."
    assert session.manifest["problem"] == " ".join(
        ["The tab was closed.", sentence.format("microphone"), sentence.format("system")]
    )
    assert session.manifest["problemKind"] == "io"


@pytest.mark.parametrize("kind", ["microphone", "system"])
@pytest.mark.parametrize("follower", [False, True])
def test_browser_deselection_drains_before_ending_track_and_keeps_known_loss(tmp_path, kind, follower):
    from test_tap_stall import DrainingTaps, in_thread, start_capture, tone

    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="system")
    identifier, tap = capture.native_taps[kind]
    session = capture.host.session
    start = capture._tap_states[identifier]["at"]
    tap.append(tone(480), start)
    admission = tap.writer_queue = NativeQueue(capacity_frames=480)
    tap.seal = admission.seal
    assert admission.enter(0, 480, start)
    admission.take(0)
    assert not admission.enter(9_000_000, 480, start + 9)
    admission.done(480)
    if follower:
        assert admission.enter(19_000_000, 480, start + 19)
    clock.ms = start + 25
    changer, result = in_thread("deselect", lambda: capture.select(kind != "microphone", kind != "system"))
    try:
        assert tap.draining.wait(BOUND), "Deselection never began draining the retiring tap"
        if follower:
            frames, lost = admission.take(19_000_000)
            tap.append(tone(frames), start + 19, writer_frames=lost)
            admission.done(frames)
        tap.drained.set()
        changer.join(BOUND)
        assert not changer.is_alive() and "error" not in result, result
        for remaining in taps.taps.values():
            remaining.drained.set()
        clock.ms = start + 50
        capture.stop()
        saved = json.loads((capture.directory / "session.json").read_text())
        track = next(t for t in saved["tracks"] if t["id"] == identifier)
        assert track["droppedFrames"] == 480
        assert not admission.enter(30_000_000, 480, start + 30)
        assert track["frames"] == (1248 if follower else 960)
        assert track["gaps"] == [dict(atMs=10, durationMs=10, frames=480, kind="writer")]
        assert track["ended"] and track["endOffsetMs"] == pytest.approx(start + (26 if follower else 20))
        assert (saved["state"], saved["problemKind"], saved["events"][-1]["reason"]) == ("interrupted", "io", "error")
        with wave.open(str(session.directory / track["file"])) as wav:
            data = wav.readframes(wav.getnframes())
        assert data[960:1920] == bytes(960)
        assert data[:960] == tone(480)
        if follower:
            assert data[1920:] == tone(288)
    finally:
        for remaining in taps.taps.values():
            remaining.drained.set()
        changer.join(BOUND)
        capture.close()


@pytest.mark.parametrize("kind", ["microphone", "system"])
@pytest.mark.parametrize("follower", [False, True])
@pytest.mark.parametrize("fail_change", [False, True])
def test_native_recorder_deselection_preserves_known_queue_tail(tmp_path, monkeypatch, kind, follower, fail_change):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from cassini_talk.native_recorder import NativeRecorder

    Gst.init(None)
    recorder = NativeRecorder()
    session = recorder.session = RecordingSession(tmp_path)
    session.manifest["events"] = [dict(type="selection", atMs=0, me=True, others=True, reason="start")]
    for side in ("microphone", "system"):
        session.add_track(side, "Me" if side == "microphone" else "The others", side, 48000, 1)
    pipeline = recorder.pipeline = Gst.Pipeline.new(None)
    recorder._test_sources = True
    recorder._stop_requested = recorder._discard = False
    parse = Gst.parse_bin_from_description
    monkeypatch.setattr(
        Gst,
        "parse_bin_from_description",
        lambda description, *args: parse("appsrc name=source format=time ! " + description.split(" ! ", 1)[1], *args),
    )
    branch = recorder._branch(kind)
    recorder._branches[kind] = branch
    pipeline.add(branch)
    source = branch.get_by_name("source")
    source.set_property(
        "caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved")
    )
    written = threading.Event()
    original = session.append_native

    def appended(*args, **kwargs):
        original(*args, **kwargs)
        written.set()

    monkeypatch.setattr(session, "append_native", appended)
    monkeypatch.setattr(recorder, "_elapsed_ms", lambda _pipeline: 25)
    release = threading.Event()
    try:
        pipeline.set_state(Gst.State.PLAYING)
        buffer = Gst.Buffer.new_wrapped(b"\x01\x00" * 480)
        buffer.pts, buffer.duration = 0, 10 * Gst.MSECOND
        assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK
        assert written.wait(BOUND)
        drained = threading.Event()
        branch.get_by_name("tap-queue").get_static_pad("src").add_probe(
            Gst.PadProbeType.IDLE, lambda *_: (drained.set(), Gst.PadProbeReturn.REMOVE)[1]
        )
        assert drained.wait(BOUND)
        admission = branch._writer_queue
        admission.capacity = 480
        assert admission.enter(1, 480, 0)
        admission.take(1)
        assert not admission.enter(9_000_000, 480, 9)
        admission.done(480)
        if fail_change:
            before = (session.directory / "session.json").read_bytes()
            state = branch.get_state(0)[1]
            checkpoint = session.checkpoint

            def refused():
                raise OSError("Generated selection checkpoint failure")

            with monkeypatch.context() as patch:
                patch.setattr(session, "checkpoint", refused)
                with pytest.raises(OSError, match="selection checkpoint"):
                    recorder._select(kind != "microphone", kind != "system")
            assert (session.directory / "session.json").read_bytes() == before
            assert branch.get_state(0)[1] == state and recorder._branches[kind] is branch
            assert session.checkpoint == checkpoint
        if follower:
            queued, sealed = threading.Event(), threading.Event()

            def hold(_pad, _info):
                queued.set()
                assert release.wait(BOUND)
                return Gst.PadProbeReturn.REMOVE

            branch.get_by_name("tap-queue").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, hold)
            seal = admission.seal

            def observed_seal(at):
                seal(at)
                sealed.set()

            monkeypatch.setattr(admission, "seal", observed_seal)
            buffer = Gst.Buffer.new_wrapped(b"\x01\x00" * 480)
            buffer.pts, buffer.duration = 19 * Gst.MSECOND, 10 * Gst.MSECOND
            assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK
            assert queued.wait(BOUND)
        changing = recorder.select(kind != "microphone", kind != "system")
        if follower:
            assert sealed.wait(BOUND), "Deselection never sealed the retiring branch"
            assert not next(t for t in session.manifest["tracks"] if t["id"] == kind).get("ended")
            release.set()
        changing.result(BOUND)
        assert not admission.enter(30_000_000, 480, 30)
        session.finish(reason="user", at_ms=50)
        saved = json.loads((session.directory / "session.json").read_text())
        track = next(t for t in saved["tracks"] if t["id"] == kind)
        assert track["droppedFrames"] == 480
        assert track["frames"] == (1248 if follower else 960) and track["ended"]
        assert track["endOffsetMs"] == (26 if follower else 20)
        assert track["gaps"] == [dict(atMs=10, durationMs=10, frames=480, kind="writer")]
        assert (saved["state"], saved["problemKind"], saved["events"][-1]["reason"]) == ("interrupted", "io", "error")
    finally:
        release.set()
        pipeline.set_state(Gst.State.NULL)
        session.finish()
        recorder.pipeline = None
        recorder.close()
