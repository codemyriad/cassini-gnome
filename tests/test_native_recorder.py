# SPDX-License-Identifier: GPL-3.0-or-later
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
from cassini_talk.native_recorder import space_left  # noqa: E402
from cassini_talk.recording_session import ProtocolError, RecordingSession  # noqa: E402


def test_space_estimate_keeps_larger_of_gib_or_two_percent_reserve(tmp_path):
    gib = 1024**3

    def stats(total, free):
        return lambda _: SimpleNamespace(f_frsize=1, f_blocks=total, f_bavail=free)

    assert space_left(tmp_path, 1024, stats(10 * gib, gib + 1024 * 60 * 29)) == (29, False)
    assert space_left(tmp_path, 1024, stats(100 * gib, 2 * gib)) == (0, True)
    assert space_left(tmp_path, 1024, stats(10 * gib, gib - 1)) == (0, True)


def test_native_named_tracks_remain_protocol_compatible_and_checkpointed(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("microphone", "Me", "microphone", 48000, 1, filename="track-microphone.wav")
    session.append("microphone", bytes(960), 25)
    session.finish()
    assert (session.directory / "track-microphone.wav").stat().st_size == 1004
    assert session.manifest["tracks"][0]["startOffsetMs"] == 25
    assert session.manifest["tracks"][0]["ended"] is True
    with pytest.raises(ProtocolError):
        other = RecordingSession(tmp_path, source="gnome")
        other.add_track("system", None, "system", 48000, 1, filename="../outside.wav")


def test_stop_during_preparation_never_starts_audio_capture(tmp_path, monkeypatch):
    import time
    from concurrent.futures import Future

    from gi.repository import GLib

    from cassini_talk.native_recorder import NativeRecorder

    recorder = NativeRecorder()
    session, space = recorder._prepare(tmp_path)
    prepared = Future()
    prepared.set_result((session, space))
    recorder.starting = True
    recorder._stop_requested = False
    events = []
    recorder.connect("started", lambda *_: events.append("started"))
    recorder.connect("finished", lambda *_: events.append("finished"))
    recorder.stop()
    monkeypatch.setattr(
        "cassini_talk.native_recorder.Gst.parse_launch", lambda _: pytest.fail("Capture started after Stop")
    )
    recorder._prepared(prepared, True)
    deadline = time.monotonic() + 3
    while "finished" not in events and time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    assert events == ["finished"]
    assert recorder.pipeline is None and all(t["frames"] == 0 for t in session.manifest["tracks"])
    recorder._executor.shutdown()


def test_stop_during_replacement_first_sample_keeps_the_confirmed_tracks(tmp_path, monkeypatch):
    import threading
    import time

    from test_recorder import spin_until

    from cassini_talk.native_recorder import NativeRecorder

    recorder = NativeRecorder()
    errors = []
    recorder.connect("error", lambda _recorder, message: errors.append(message))
    entered, release = threading.Event(), threading.Event()
    try:
        recorder.start(tmp_path, test=True)
        spin_until(
            lambda: (
                recorder.session is not None and all(t["frames"] >= 9600 for t in recorder.session.manifest["tracks"])
            )
        )
        first = recorder.select(True, False)
        spin_until(lambda: first.done() and not recorder._changing_sources)
        first.result()
        session = recorder.session
        events, tracks = list(session.manifest["events"]), set(session.writers)
        sample = recorder._sample

        def delayed_first_sample(sink, kind):
            entered.set()
            assert release.wait(3)
            return sample(sink, kind)

        monkeypatch.setattr(recorder, "_sample", delayed_first_sample)
        replacement = recorder.select(False, True)
        spin_until(entered.is_set)
        assert not replacement.done()
        started = time.monotonic()
        recorder.stop()
        assert time.monotonic() - started < 0.2
        release.set()
        spin_until(lambda: recorder.pipeline is None and not recorder._finishing)
        with pytest.raises(ValueError, match="No source change"):
            replacement.result()
        assert session.manifest["events"][:-1] == events and set(session.writers) == tracks
        assert session.manifest["events"][-1]["type"] == "end"
        assert session.manifest["events"][-1]["reason"] == "user"
        assert session.manifest["state"] == "completed" and not errors
        assert not recorder._pending_sources
        assert len(list(session.directory.glob("track-*.wav"))) == len(tracks)
    finally:
        release.set()
        recorder.stop()
        spin_until(lambda: recorder.pipeline is None and not recorder._finishing)
        recorder._executor.shutdown()


def test_preparation_stop_end_time_uses_this_start_not_an_old_recorder_clock(tmp_path, monkeypatch):
    from test_recorder import spin_until

    from cassini_talk.native_recorder import NativeRecorder

    # A folder kept for typed content (D58 discardBlank) is finished as before.
    monkeypatch.setattr(RecordingSession, "discard", lambda _session: False)
    recorder = NativeRecorder()
    recorder._start_us = 0
    recorder.start(tmp_path, test=True)
    recorder.stop()  # Before the prepared result reaches the main context.
    try:
        spin_until(lambda: recorder.session is not None and recorder.session.finished and not recorder._finishing)
        assert recorder.pipeline is None and all(t["frames"] == 0 for t in recorder.session.manifest["tracks"])
        end = recorder.session.manifest["events"][-1]
        assert end["reason"] == "user" and 0 <= end["atMs"] < 5000
    finally:
        recorder._executor.shutdown()


@pytest.mark.parametrize("empty", ["microphone", "system"])
def test_stop_keeps_healthy_pcm_when_an_empty_source_never_acknowledges_eos(tmp_path, monkeypatch, empty):
    from gi.repository import Gst

    from cassini_talk.native_recorder import NativeRecorder

    Gst.init(None)
    recorder = NativeRecorder()
    branch = recorder._branch
    errors, finished, devices = [], [], {}
    recorder.connect("error", lambda _r, value: errors.append(value))
    recorder.connect("finished", lambda _r, value: finished.append(value))

    def stalled(kind):
        if kind != empty:
            return branch(kind)
        result = Gst.parse_bin_from_description(
            "appsrc name=unresponsive is-live=true format=time do-timestamp=true "
            'caps="audio/x-raw,format=S16LE,rate=48000,channels=1" '
            f"! queue ! appsink name={kind} emit-signals=true sync=false async=false",
            False,
        )
        devices[kind] = result.get_by_name("unresponsive")

        # Model a source that never emits a frame or acknowledges the EOS.
        def no_end(_pad, info):
            return Gst.PadProbeReturn.DROP if info.get_event().type == Gst.EventType.EOS else Gst.PadProbeReturn.OK

        result.get_by_name("unresponsive").get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, no_end)
        result.get_by_name(kind).connect("new-sample", recorder._sample, kind)
        return result

    import time

    from test_recorder import spin_until

    def select(me, others):
        future = recorder.select(me, others)
        spin_until(lambda: future.done() and not recorder._changing_sources)
        return future.result()

    healthy = "system" if empty == "microphone" else "microphone"
    try:
        recorder.start(tmp_path, test=True)
        spin_until(lambda: not recorder.starting and len(recorder._track_ids) == 2)
        # A start side records from its first buffer, so only a turn-on can
        # leave an empty track: its readiness buffer is not recorded. Turn the
        # side off, then on again on a source that sends that one buffer only.
        select(empty != "microphone", empty != "system")
        monkeypatch.setattr(recorder, "_branch", stalled)
        turning_on = recorder.select(True, True)
        spin_until(lambda: empty in recorder._pending_sources)
        assert devices[empty].emit("push-buffer", Gst.Buffer.new_wrapped(bytes(960))) == Gst.FlowReturn.OK
        spin_until(lambda: turning_on.done() and not recorder._changing_sources)
        turning_on.result()
        identifier = recorder._track_ids[empty]
        spin_until(lambda: recorder.session.writers[recorder._track_ids[healthy]].frames >= 9600)
        saved = (recorder.session.directory / f"track-{healthy}.wav").read_bytes()[44:]
        started = time.monotonic()
        recorder.stop()
        assert time.monotonic() - started < 0.2
        spin_until(lambda: bool(finished), timeout=3)
        assert not errors
        assert recorder.session.manifest["state"] == "completed"
        assert all(track["ended"] for track in recorder.session.manifest["tracks"])
        assert recorder.session.writers[identifier].frames == 0
        assert saved and (recorder.session.directory / f"track-{healthy}.wav").read_bytes()[44:].startswith(saved)
    finally:
        if recorder.pipeline is not None:
            recorder._begin_finish()
            spin_until(lambda: recorder.pipeline is None)
        recorder._executor.shutdown()
