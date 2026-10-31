# SPDX-License-Identifier: GPL-3.0-or-later
"""Mute is an observed output property, never a guess from silent PCM."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from cassini_talk.output_mute import OutputState, read_output_state


def reply(monkeypatch, sinks):
    def run(command, **options):
        # Only a bounded, read-only request; a mute check must not reroute,
        # unmute, or open a recording stream to light a privacy indicator.
        assert command == ["pactl", "--format=json", "list", "sinks"]
        assert options["timeout"] == 1 and options["check"]
        return SimpleNamespace(stdout=json.dumps(sinks))

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("muted", [True, False])
def test_uses_the_captured_monitor_even_if_a_different_output_is_default(monkeypatch, muted):
    reply(
        monkeypatch,
        [
            dict(name="speakers", monitor_source="speakers.monitor", mute=not muted),
            dict(name="headphones", monitor_source="headphones.monitor", mute=muted),
        ],
    )
    assert read_output_state("headphones.monitor") == OutputState("headphones.monitor", "headphones", muted)


@pytest.mark.parametrize("value", [None, "", "mic", "missing.monitor"])
def test_missing_or_non_monitor_source_is_unknown(monkeypatch, value):
    reply(monkeypatch, [dict(name="speakers", monitor_source="speakers.monitor", mute=True)])
    assert read_output_state(value) is None


@pytest.mark.parametrize(
    "sinks", [None, {}, [None], [dict(name="speakers", monitor_source="speakers.monitor", mute=1)]]
)
def test_bad_server_metadata_never_claims_an_output_is_unmuted(monkeypatch, sinks):
    reply(monkeypatch, sinks)
    assert read_output_state("speakers.monitor") is None


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.TimeoutExpired("pactl", 1), subprocess.CalledProcessError(1, "pactl")]
)
def test_missing_tool_and_failed_or_timed_out_server_are_unknown(monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert read_output_state("speakers.monitor") is None


def test_history_records_only_transitions_and_preserves_unrelated_edits(tmp_path):
    from cassini_talk.meeting_store import read_metadata, update_metadata
    from cassini_talk.output_mute import MuteHistory, currently_muted, intervals
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    history = MuteHistory(session.directory)
    update_metadata(session.directory, recordingDetails=dict(future=42, audioHealth=dict(future="kept")))
    history.observe("system", "one.monitor", True, 0)
    first = read_metadata(session.directory)
    assert currently_muted(first)
    history.observe("system", "one.monitor", True, 100)
    assert read_metadata(session.directory) == first  # No per-tick rewrite.
    update_metadata(session.directory, title="Edited during recording", titleSource="user")
    history.observe("system", "one.monitor", None, 200)  # Unknown is never unmuted.
    history.observe("system", "one.monitor", False, 300)
    history.observe("system", "two.monitor", True, 500)
    history.observe("system-2", "two.monitor", True, 700)  # Fresh fallback span.
    history.observe(None, None, None, 900)  # Off / restored / stopped.
    result = read_metadata(session.directory)
    assert result["title"] == "Edited during recording"
    assert result["recordingDetails"]["future"] == 42
    assert result["recordingDetails"]["audioHealth"]["future"] == "kept"
    assert intervals(result) == [
        dict(trackId="system", startMs=0, endMs=300),
        dict(trackId="system", startMs=500, endMs=700),
        dict(trackId="system-2", startMs=700, endMs=900),
    ]
    assert not currently_muted(result)
    session.finish()


@pytest.mark.parametrize(
    "rows", [None, {}, [None], [dict(trackId="s", startMs=True)], [dict(trackId="s", startMs=2, endMs=1)]]
)
def test_untrusted_facts_are_not_mute_evidence(rows):
    from cassini_talk.output_mute import intervals

    assert intervals(dict(recordingDetails=dict(audioHealth=dict(outputMuted=rows)))) == []


def test_worker_waits_for_explicit_refresh_and_stop_without_a_fixed_test_wait(tmp_path):
    import threading

    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.output_mute import OutputMuteObserver, intervals
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    state = dict(track="system", monitor="sink.monitor", muted=True, at=10)
    applied = threading.Event()
    worker = OutputMuteObserver(
        session.directory,
        lambda: (state["track"], state["monitor"], state["at"]),
        lambda _muted: applied.set(),
        query=lambda monitor: OutputState(monitor, "sink", state["muted"]),
    )
    worker.start()
    try:
        assert applied.wait(3), "Muted-output observation was not persisted"
        assert intervals(read_metadata(session.directory)) == [dict(trackId="system", startMs=10)]
        applied.clear()
        state.update(muted=False, at=100)
        worker.refresh()
        assert applied.wait(3), "Unmute observation was not persisted"
        assert intervals(read_metadata(session.directory)) == [dict(trackId="system", startMs=10, endMs=100)]
        applied.clear()
        state.update(muted=True, at=200)
        worker.refresh()
        assert applied.wait(3), "Second muted span was not persisted"
    finally:
        worker.close(300)
        session.finish()
    assert not worker.thread.is_alive(), "Mute observer survived Stop"
    assert intervals(read_metadata(session.directory))[-1] == dict(trackId="system", startMs=200, endMs=300)


def test_failed_observation_does_not_end_capture_and_next_refresh_can_recover(tmp_path):
    import threading

    from cassini_talk.output_mute import OutputMuteObserver
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    failed, applied, retry = threading.Event(), threading.Event(), threading.Event()

    def query(monitor):
        if not retry.is_set():
            failed.set()
            raise OSError("Synthetic server unavailable")
        return OutputState(monitor, "sink", True)

    observer = OutputMuteObserver(
        session.directory,
        lambda: ("system", "sink.monitor", 20),
        lambda muted: applied.set() if muted else None,
        query=query,
    )
    observer.start()
    try:
        assert failed.wait(3), "The failing server query was not attempted"
        assert not session.finished, "Mute observation failure stopped capture"
        retry.set()
        observer.refresh()
        assert applied.wait(3), "Observer did not recover after the server returned"
    finally:
        observer.close(30)
        session.finish()


def _metadata_process(directory, role, ready, release, waiting):
    """Spawned broker/GTK analogue; synchronize at the atomic commit/flock."""
    import fcntl

    from cassini_talk import meeting_store
    from cassini_talk.output_mute import MuteHistory

    if ready is not None:
        original = meeting_store.atomic_json

        def staged(path, value, **kwargs):
            def before_commit():
                ready.set()
                assert release.wait(8), "Parent did not release the staged metadata commit"

            return original(path, value, before_commit=before_commit)

        meeting_store.atomic_json = staged
    else:
        original_flock = fcntl.flock

        def entering_lock(descriptor, operation):
            if operation == fcntl.LOCK_EX:
                waiting.set()
            return original_flock(descriptor, operation)

        fcntl.flock = entering_lock
    if role == "rename":
        meeting_store.update_metadata(directory, title="Changed in GTK", titleSource="user")
    else:
        history = MuteHistory(directory)
        history.observe("system", "sink.monitor", True, 10)
        history.observe("system", "sink.monitor", False, 200)
        assert history.active is None


@pytest.mark.parametrize("first", ["rename", "history"])
@pytest.mark.parametrize("managed", [False, True])
def test_metadata_edits_serialize_across_processes_at_commit(tmp_path, first, managed):
    import multiprocessing

    from cassini_talk.library_publication import capture_parent
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.output_mute import intervals
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(capture_parent(tmp_path) if managed else tmp_path)
    ctx = multiprocessing.get_context("spawn")
    ready, release, waiting = ctx.Event(), ctx.Event(), ctx.Event()
    one = ctx.Process(target=_metadata_process, args=(session.directory, first, ready, release, None))
    two = ctx.Process(
        target=_metadata_process,
        args=(session.directory, "history" if first == "rename" else "rename", None, release, waiting),
    )
    try:
        one.start()
        assert ready.wait(8), "First process did not stage its metadata replacement"
        two.start()
        assert waiting.wait(8), "Second process never entered the cross-process metadata lock"
        release.set()
        for process in (one, two):
            process.join(8)
            assert not process.is_alive(), "Metadata writer did not finish after lock release"
            assert process.exitcode == 0
        metadata = read_metadata(session.directory)
        assert metadata["title"] == "Changed in GTK"
        assert intervals(metadata) == [dict(trackId="system", startMs=10, endMs=200)]
    finally:
        release.set()
        for process in (one, two):
            if process.pid is not None:
                process.join(2)
                if process.is_alive():
                    process.terminate()
                    process.join(2)
        session.finish()


def test_metadata_lock_rejects_symlink(tmp_path):
    from cassini_talk.meeting_store import update_metadata
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    target = tmp_path / "foreign"
    target.write_bytes(b"Do not lock or edit")
    (session.directory / ".meeting.lock").symlink_to(target)
    try:
        with pytest.raises(OSError):
            update_metadata(session.directory, title="Unsafe")
        assert target.read_bytes() == b"Do not lock or edit"
        assert not (session.directory / "meeting.json").exists()
    finally:
        session.finish()


def test_retirement_uses_commit_time_even_with_a_delayed_stale_query(tmp_path):
    import threading

    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.output_mute import OutputMuteObserver, intervals
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(tmp_path)
    applied, querying, release, ended = (threading.Event() for _ in range(4))
    block = threading.Event()

    def query(monitor):
        if block.is_set():
            querying.set()
            assert release.wait(5), "Stale Pulse query was not released"
        return OutputState(monitor, "sink", True)

    observer = OutputMuteObserver(
        session.directory,
        lambda: ("system", "sink.monitor", 900 if block.is_set() else 10),
        lambda muted: (applied if muted else ended).set(),
        query=query,
    )
    observer.start()
    try:
        assert applied.wait(3), "Initial mute interval missing"
        block.set()
        observer.refresh()
        assert querying.wait(3), "Delayed query never started"
        observer.retire("system", 120)
        release.set()
        assert ended.wait(3), "Committed retirement was not persisted"
        assert intervals(read_metadata(session.directory)) == [dict(trackId="system", startMs=10, endMs=120)]
    finally:
        release.set()
        observer.close(1000)
        session.finish()
    assert intervals(read_metadata(session.directory)) == [dict(trackId="system", startMs=10, endMs=120)]


@pytest.mark.parametrize("kind", ["native", "browser"])
def test_delayed_device_query_does_not_block_pcm_or_error_handling(tmp_path, monkeypatch, kind):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Capture lock regressions require the GStreamer typelib")
    from gi.repository import GLib, Gst

    Gst.init(None)

    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.native_recorder import NativeRecorder
    from cassini_talk.recording_session import RecordingSession

    entered, release, progressed = threading.Event(), threading.Event(), threading.Event()
    session = RecordingSession(tmp_path)
    session.add_track("system", "The others", "system", 48000, 1)
    if kind == "native":
        capture = NativeRecorder()
        capture._stop_requested = False
        capture.session, capture.pipeline = session, SimpleNamespace(get_base_time=lambda: 0)
        capture._start_us = GLib.get_monotonic_time()
        capture._track_ids = {"system": "system"}
        capture._branches = {"system": object()}
        capture._boundaries = {"system": 0}
        error_handled = threading.Event()
        monkeypatch.setattr(capture, "_begin_finish", lambda *_: error_handled.set())
        buffer = Gst.Buffer.new_allocate(None, 960, None)
        buffer.fill(0, bytes(960))
        buffer.pts = 0
        sample = Gst.Sample.new(buffer, None, None, None)

        def pcm_and_error():
            assert capture._sample(SimpleNamespace(emit=lambda _: sample), "system") == Gst.FlowReturn.OK
            progressed.set()

        def retire():
            with capture._lock:
                capture._track_ids.clear()
                capture._branches.clear()

    else:

        class Tap:
            def __init__(self, kind, origin, append, failed, *, level):
                self.append, self.failed, self.pipeline = append, failed, object()

        capture = BrowserCapture(lambda *_: None, tap_factory=Tap)
        capture.host.session = session
        capture.origin_ms = 0
        tap, _state = capture._make_native("system", "system", active=True)
        capture.native_taps["system"] = "system", tap

        def pcm_and_error():
            tap.append(bytes(960), 0)
            tap.failed("Synthetic error")
            assert capture.error == "Synthetic error"
            progressed.set()

        def retire():
            with capture.lock:
                capture.native_taps.clear()

    def device(_branch):
        entered.set()
        assert release.wait(5), "Test did not release the delayed Pulse query"
        return "sink.monitor"

    monkeypatch.setattr("cassini_talk.output_mute.monitor_device", device)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(capture._output_sample)
        try:
            assert entered.wait(3), "Device query did not start"
            pcm = executor.submit(pcm_and_error)
            assert progressed.wait(3), "Device query blocked the PCM/error-handler capture lock"
            pcm.result()
            if kind == "native":
                # Dispatch the GTK bus-handler path on the main test thread,
                # while the Pulse query is still blocked on the worker.
                capture._message(
                    None,
                    SimpleNamespace(
                        type=Gst.MessageType.ERROR,
                        parse_error=lambda: (SimpleNamespace(message="Synthetic error"), None),
                        src=object(),
                    ),
                )
                assert error_handled.is_set()
            assert session.manifest["tracks"][0]["frames"] == 480
            retire()
        finally:
            release.set()
        result = pending.result(timeout=3)
        assert capture._output_validate(result) is None, "Retired source's delayed result was accepted"
    session.finish()
    if kind == "native":
        capture._executor.shutdown()


@pytest.mark.parametrize("kind", ["native", "browser"])
def test_source_change_closes_history_before_slow_device_shutdown(tmp_path, monkeypatch, kind):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Capture lock regressions require the GStreamer typelib")
    from gi.repository import GLib, Gst

    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.native_recorder import NativeRecorder
    from cassini_talk.output_mute import OutputMuteObserver, intervals
    from cassini_talk.recording_session import RecordingSession

    Gst.init(None)
    applied, ended, stopping, release = (threading.Event() for _ in range(4))
    monkeypatch.setattr("cassini_talk.output_mute.monitor_device", lambda _: "sink.monitor")
    if kind == "native":
        session = RecordingSession(tmp_path)
        for track in ("microphone", "system"):
            session.add_track(track, track, track, 48000, 1)
            session.append(track, bytes(960), 0)

        class Branch:
            def set_state(self, _state):
                stopping.set()
                assert release.wait(5), "Test did not release device shutdown"

        capture = NativeRecorder()
        capture._stop_requested = False
        capture.session = session
        capture._start_us = GLib.get_monotonic_time() - 10_000
        capture._track_ids = {"microphone": "microphone", "system": "system"}
        capture._branches = {"microphone": object(), "system": Branch()}
        capture.pipeline = SimpleNamespace(
            get_clock=lambda: SimpleNamespace(get_time=lambda: 1000 * Gst.MSECOND),
            get_base_time=lambda: 0,
            remove=lambda _: None,
        )

        def change():
            return capture._select(True, False)
    else:

        class Tap:
            def __init__(self, kind, origin, append, failed, *, level):
                self.kind, self.append, self.pipeline = kind, append, object()

            def start(self):
                self.append(bytes(960), 0)

            def stop(self):
                if self.kind == "system":
                    stopping.set()
                    assert release.wait(5), "Test did not release tap shutdown"

        capture = BrowserCapture(lambda *_: None, parent=tmp_path, tap_factory=Tap)
        capture.start(
            dict(
                type="start",
                version=1,
                independent=True,
                captureId="retirement",
                timelineOriginMs=int(time.time() * 1000),
                microphone=True,
                title="Retirement",
            ),
            others_source="system",
        )
        session = capture.host.session

        def change():
            return capture.select(True, False)

    observer = capture._mute_observer = OutputMuteObserver(
        session.directory,
        capture._output_sample,
        lambda muted: (applied if muted else ended).set(),
        query=lambda monitor: OutputState(monitor, "sink", True),
        validate=capture._output_validate,
    )
    observer.start()
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            assert applied.wait(3), "Initial mute interval was not persisted"
            future = executor.submit(change)
            if not stopping.wait(3):
                if future.done():
                    future.result()
                pytest.fail("Device retirement did not reach its slow shutdown")
            assert ended.wait(3), "History waited for device shutdown instead of the committed retirement"
            event = next(e for e in reversed(session.manifest["events"]) if e["type"] == "othersSource")
            assert event["source"] == "none"
            rows = intervals(read_metadata(session.directory))
            assert len(rows) == 1 and rows[0]["endMs"] == event["atMs"]
        finally:
            release.set()
            observer.close(5000)
        future.result(timeout=3)
    session.finish()
    if kind == "native":
        capture._executor.shutdown()
