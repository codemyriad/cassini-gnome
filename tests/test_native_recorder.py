# SPDX-License-Identifier: GPL-3.0-or-later
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
from cassini_viewer.native_recorder import space_left  # noqa: E402
from cassini_viewer.recording_session import ProtocolError, RecordingSession  # noqa: E402


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

    from cassini_viewer.native_recorder import NativeRecorder

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
        "cassini_viewer.native_recorder.Gst.parse_launch", lambda _: pytest.fail("Capture started after Stop")
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

    from cassini_viewer.native_recorder import NativeRecorder

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
