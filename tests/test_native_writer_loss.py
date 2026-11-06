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


def _native_deselection_case(tmp_path, monkeypatch, kind, follower, fail_change, *, stop_during_drain=False):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from cassini_talk.native_recorder import NativeRecorder

    Gst.init(None)
    recorder = NativeRecorder()
    errors = []
    recorder.connect("error", lambda _, problem: errors.append(problem))
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
        if stop_during_drain:
            entered = threading.Event()

            def timed_out_drain(_kind, retiring):
                entered.set()
                assert release.wait(BOUND)
                retiring.set_state(Gst.State.NULL)
                return False  # Inject the drain-timeout outcome, not a real delay.

            monkeypatch.setattr(recorder, "_drain_branch", timed_out_drain)
        changing = recorder.select(kind != "microphone", kind != "system")
        if follower:
            assert sealed.wait(BOUND), "Deselection never sealed the retiring branch"
            assert not next(t for t in session.manifest["tracks"] if t["id"] == kind).get("ended")
            release.set()
        if stop_during_drain:
            assert entered.wait(BOUND)
            monkeypatch.setattr(recorder, "_elapsed_ms", lambda _: 40)
            recorder.stop()
            recorder._timed_out()
            assert recorder._stop_requested and recorder._stop_at_ms == 40
            release.set()
        changing.result(BOUND)
        assert not admission.enter(30_000_000, 480, 30)
        if stop_during_drain:
            from gi.repository import GLib

            loop, finished = GLib.MainLoop(), []
            recorder.connect("finished", lambda *_: (finished.append(True), loop.quit()))
            timer = GLib.timeout_add_seconds(BOUND, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
            loop.run()
            if finished:
                GLib.source_remove(timer)
            assert finished, "Stop after the deselection did not finish"
        else:
            session.finish(reason="user", at_ms=50)
        saved = json.loads((session.directory / "session.json").read_text())
        track = next(t for t in saved["tracks"] if t["id"] == kind)
        if stop_during_drain:
            assert track["droppedFrames"] == 768 and track["frames"] == 1248
            assert track["ended"] and track["endOffsetMs"] == 26
            assert saved["events"][-1]["atMs"] == 40
            assert not errors
            from cassini_talk.writer_loss import writer_only

            assert writer_only(saved), saved.get("problem")
            assert next(e for e in reversed(saved["events"]) if e["type"] == "selection")["atMs"] == 25
        else:
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


@pytest.mark.parametrize("kind", ["microphone", "system"])
def test_stop_during_browser_deselection_timeout_flushes_retiring_loss_once(tmp_path, kind):
    from test_tap_stall import DrainingTaps, in_thread, start_capture, tone

    taps = DrainingTaps()
    capture, clock = start_capture(tmp_path, taps, microphone=True, others_source="system")
    identifier, tap = capture.native_taps[kind]
    tap.append(tone(480), 0)
    admission = tap.writer_queue = NativeQueue(capacity_frames=480)
    tap.seal, tap.timed_out = admission.seal, True
    assert admission.enter(0, 480, 0)
    admission.take(0)
    assert not admission.enter(9_000_000, 480, 9)
    admission.done(480)
    clock.ms = 25
    changer, changed = in_thread("deselect", lambda: capture.select(kind != "microphone", kind != "system"))
    stopper = None
    try:
        assert tap.draining.wait(BOUND)
        clock.ms = 40
        stopper, stopped = in_thread("stop", capture.stop)
        assert capture.stop_requested.wait(BOUND)
        for source in taps.taps.values():
            source.drained.set()
        changer.join(BOUND)
        stopper.join(BOUND)
        assert not changer.is_alive() and "error" not in changed, changed
        assert not stopper.is_alive() and "error" not in stopped, stopped
        saved = json.loads((capture.directory / "session.json").read_text())
        track = next(t for t in saved["tracks"] if t["id"] == identifier)
        # 10 ms received + 10 ms known loss + 6 ms inferred (host 19..25).
        # Stop at 40 must never flush host 19..40 over that loss a second time.
        assert track["droppedFrames"] == 768 and track["frames"] == 1248
        assert track["ended"] and track["endOffsetMs"] == 26
        assert saved["events"][-1]["atMs"] == 40
        selection = next(e for e in reversed(saved["events"]) if e["type"] == "selection")
        assert selection == dict(
            type="selection", atMs=25, reason="user", me=kind != "microphone", others=kind != "system"
        )
    finally:
        for source in taps.taps.values():
            source.drained.set()
        changer.join(BOUND)
        if stopper is not None:
            stopper.join(BOUND)
        capture.close()


@pytest.mark.parametrize("kind", ["microphone", "system"])
@pytest.mark.parametrize("first_ms", [10, 30])
@pytest.mark.parametrize("turn_on_ms", [0, 15])
@pytest.mark.parametrize("stop_during_drain", [False, True])
def test_deselecting_armed_input_preserves_pre_cut_audio_and_releases_eos(
    tmp_path, monkeypatch, kind, first_ms, turn_on_ms, stop_during_drain
):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from cassini_talk.native_recorder import NativeRecorder

    Gst.init(None)
    recorder = NativeRecorder()
    session = recorder.session = RecordingSession(tmp_path)
    other = "system" if kind == "microphone" else "microphone"
    session.add_track(other, "Other selected side", other, 48000, 1)
    session.append_native(other, b"\x01\x00" * 480, 0)
    session.manifest["events"] = [dict(type="selection", atMs=0, me=True, others=True, reason="start")]
    recorder._track_ids = {other: other}
    recorder._stop_requested = recorder._discard = False
    pipeline = recorder.pipeline = Gst.Pipeline.new(None)
    recorder._test_sources = True
    parse = Gst.parse_bin_from_description
    monkeypatch.setattr(
        Gst,
        "parse_bin_from_description",
        lambda description, *args: parse("appsrc name=source format=time ! " + description.split(" ! ", 1)[1], *args),
    )
    branch = recorder._branch(kind)
    pipeline.add(branch)
    sink, source = branch.get_by_name(kind), branch.get_by_name("source")
    source.set_property(
        "caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved")
    )
    pad = sink.get_static_pad("sink")
    hold = pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, recorder._hold_eos)
    attempt = dict(
        branch=branch,
        pipeline=pipeline,
        armed=True,
        committed=False,
        retired=False,
        error=None,
        deadline=None,
        ready=threading.Event(),
        identifier=kind + "-late",
        refuse_at=turn_on_ms,
        filename=None,
        hold=(pad, hold),
    )
    recorder._pending_sources[kind] = attempt
    queued, release, sealed = (threading.Event() for _ in range(3))

    def gate(*_):
        queued.set()
        assert release.wait(BOUND)
        return Gst.PadProbeReturn.REMOVE

    branch.get_by_name("tap-queue").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, gate)
    admission = branch._writer_queue
    admission.capacity = 480
    rejected = threading.Event()
    enter = admission.enter

    def observed_enter(stamp, frames, offset):
        accepted = enter(stamp, frames, offset)
        if not accepted:
            rejected.set()
        return accepted

    monkeypatch.setattr(admission, "enter", observed_enter)
    seal = admission.seal

    def observed_seal(at):
        seal(at)
        sealed.set()

    monkeypatch.setattr(admission, "seal", observed_seal)
    monkeypatch.setattr(recorder, "_elapsed_ms", lambda _: 25)
    drained, drain = [], recorder._drain_branch

    def observed_drain(*args):
        result = drain(*args)
        drained.append(result)
        return result

    monkeypatch.setattr(recorder, "_drain_branch", observed_drain)
    changing = None
    try:
        pipeline.set_state(Gst.State.PLAYING)
        buffer = Gst.Buffer.new_wrapped(b"\x02\x00" * 480)
        buffer.pts, buffer.duration = first_ms * Gst.MSECOND, 10 * Gst.MSECOND
        assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK
        assert queued.wait(BOUND)
        if first_ms == 10:
            # While the first 10 ms is held before the writer, exact admission
            # rejects another 5 ms. Both callbacks were captured before the cut.
            dropped = Gst.Buffer.new_wrapped(b"\x03\x00" * 240)
            dropped.pts, dropped.duration = 20 * Gst.MSECOND, 5 * Gst.MSECOND
            assert source.emit("push-buffer", dropped) == Gst.FlowReturn.OK
            assert rejected.wait(BOUND)
        changing = recorder.select(kind != "microphone", kind != "system")
        assert sealed.wait(BOUND)
        if stop_during_drain:
            monkeypatch.setattr(recorder, "_elapsed_ms", lambda _: 40)
            recorder.stop()
            assert recorder._stop_requested and recorder._stop_at_ms == 40
        release.set()
        changing.result(2 * BOUND)
        assert drained == [True], "Armed source's EOS was held until the drain timeout"
        assert "hold" not in attempt and attempt["retired"]
        assert not recorder._pending_sources
        if stop_during_drain:
            _wait_native_finished(recorder)
        else:
            session.finish(reason="user", at_ms=50)
        if first_ms == 10:
            track = next(t for t in session.manifest["tracks"] if t["kind"] == kind)
            start = max(first_ms, turn_on_ms)
            received = (20 - start) * 48
            assert track["startOffsetMs"] == start and track["endOffsetMs"] == 25
            assert track["frames"] == received + 240 and track["droppedFrames"] == 240
            assert track["ended"]
            assert track["gaps"] == [dict(atMs=20, durationMs=5, frames=240, kind="writer")]
            with wave.open(str(session.directory / track["file"])) as audio:
                assert audio.readframes(audio.getnframes()) == b"\x02\x00" * received + bytes(480)
            from cassini_talk.writer_loss import writer_only

            assert writer_only(session.manifest)
        else:
            assert [t["kind"] for t in session.manifest["tracks"]] == [other]
            assert session.manifest["state"] == "completed" and not session.manifest.get("problemKind")
    finally:
        release.set()
        if changing is not None:
            changing.result(2 * BOUND)
        pipeline.set_state(Gst.State.NULL)
        session.finish()
        recorder.pipeline = None
        recorder.close()


@pytest.mark.parametrize("kind", ["microphone", "system"])
@pytest.mark.parametrize("follower", [False, True])
@pytest.mark.parametrize("fail_change", [False, True])
def test_native_recorder_deselection_preserves_known_queue_tail(tmp_path, monkeypatch, kind, follower, fail_change):
    _native_deselection_case(tmp_path, monkeypatch, kind, follower, fail_change)


@pytest.mark.parametrize("kind", ["microphone", "system"])
def test_stop_during_native_deselection_timeout_flushes_retiring_loss_once(tmp_path, monkeypatch, kind):
    _native_deselection_case(tmp_path, monkeypatch, kind, False, False, stop_during_drain=True)


def _wait_native_finished(recorder):
    from gi.repository import GLib

    loop, finished = GLib.MainLoop(), []
    handler = recorder.connect("finished", lambda *_: (finished.append(True), loop.quit()))
    timer = GLib.timeout_add_seconds(BOUND, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()
    if finished:
        GLib.source_remove(timer)
    recorder.disconnect(handler)
    assert finished, "Native Stop did not finish"


@pytest.mark.parametrize("loss", ["none", "known", "inferred"])
@pytest.mark.parametrize("problem", [None, "Generated audio device failure"])
def test_native_drain_timeout_reports_only_unaccounted_failures(tmp_path, loss, problem):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from types import SimpleNamespace

    from cassini_talk.native_recorder import NativeRecorder
    from cassini_talk.writer_loss import sentence, writer_only

    recorder = NativeRecorder()
    session = recorder.session = RecordingSession(tmp_path)
    session.add_track("microphone", "Me", "microphone", 48000, 1)
    session.append_native("microphone", b"\x01\x00" * 480, 0)
    timeline = recorder._timelines["microphone"] = NativeTimeline(48000)
    timeline.map(0, 480, 0)
    admission = NativeQueue(capacity_frames=480)
    writer_frames = 480 if loss != "none" else 0
    if loss == "known":
        assert admission.enter(0, 480, 0)
        admission.take(0)
        assert not admission.enter(10_000_000, writer_frames, 10)
        admission.done(480)
    recorder._branches["microphone"] = SimpleNamespace(_writer_queue=admission)
    recorder._stop_at_ms = 20 if writer_frames else 10
    recorder._problem = problem
    errors = []
    recorder.connect("error", lambda _, message: errors.append(message))
    try:
        recorder._timed_out()
        _wait_native_finished(recorder)
        saved = json.loads((session.directory / "session.json").read_text())
        expected = problem or (None if writer_frames else "Timed out while saving the recording.")
        assert errors == ([expected] if expected else [])
        if writer_frames:
            track = saved["tracks"][0]
            assert track["frames"] == 960 and track["droppedFrames"] == 480
            assert saved["problem"] == " ".join(filter(None, [expected, sentence(10, "microphone")]))
            assert writer_only(saved) is (problem is None)
        else:
            assert saved["problem"] == expected
    finally:
        recorder.close()
