# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""Real synthetic capture owners publish after Stop, never on the GTK thread."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication tests require GStreamer", allow_module_level=True)

from durable import DURABLE_BOUND, saved
from test_browser_capture import Tap, remote
from test_recorder import spin_until

from cassini_talk import library_publication
from cassini_talk.browser_capture import BrowserCapture
from cassini_talk.library import LibraryScanner
from cassini_talk.library_location import capture_busy
from cassini_talk.library_v2 import finalize
from cassini_talk.native_recorder import NativeRecorder


def recording(recorder):
    """Both test sides opened and recorded 100 ms.

    A side's track is added only when it opens (R79 1), so a check over the
    tracks alone is vacuously true while a slow start has opened none: a Stop
    then discards the blank folder instead of publishing it.
    """
    tracks = recorder.session.manifest["tracks"] if recorder.session else []
    return not recorder.starting and len(tracks) == 2 and all(t["frames"] >= 4800 for t in tracks)


def test_native_stop_keeps_main_loop_responsive_and_lease_until_published(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    entered, release = threading.Event(), threading.Event()
    original = finalize.finalize

    def slow_publish(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", slow_publish)
    recorder = NativeRecorder()
    errors, finished = [], []
    recorder.connect("error", lambda _recorder, message: errors.append(message))
    recorder.connect("finished", lambda _recorder, path: finished.append(path))
    try:
        recorder.start(test=True)
        spin_until(lambda: recording(recorder))
        sid = recorder.session.manifest["id"]
        recorder.stop()
        spin_until(entered.is_set)
        assert recorder.stopping and recorder.activity == {sid: "saving"}
        assert capture_busy(sid) and not finished
        assert not list((tmp_path / "Library").glob("*.opus"))
        release.set()
        spin_until(lambda: bool(finished))
        assert not errors and not capture_busy(sid)
        (meeting,) = LibraryScanner(tmp_path / "Library").scan().meetings
        assert meeting.audio_file.exists() and meeting.metadata["published"]
        assert meeting.capture_events[-1]["type"] == "end"
        assert recorder.publication.name == meeting.audio_file.name
    finally:
        release.set()
        if recorder.pipeline is not None and not recorder.stopping:
            recorder.stop()
        spin_until(lambda: not recorder.stopping)
        recorder.close()


def test_browser_stop_ack_does_not_wait_for_encoder_and_keeps_saving_owner(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    entered, release = threading.Event(), threading.Event()
    original = finalize.finalize

    def slow_publish(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", slow_publish)
    events = []
    capture = BrowserCapture(lambda _capture, event: events.append(event), tap_factory=Tap)
    try:
        capture.start(
            dict(
                type="start",
                version=1,
                independent=True,
                captureId="published-browser",
                timelineOriginMs=int(time.time() * 1000),
                microphone=True,
                title="Published browser",
            )
        )
        remote(capture, "remote")
        sid = capture.host.session.manifest["id"]
        result = capture.stop()
        assert result["type"] == "stopped" and entered.wait(3)
        assert capture.stopping and not capture.finished and capture_busy(sid)
        assert capture.stop()["type"] == "stopped"  # A second Stop also only joins PCM teardown.
        assert events.count("finished") == 0
        release.set()
        saved(capture)
        assert capture.finished and not capture.stopping and not capture_busy(sid)
        assert events.count("finished") == 1 and capture.publication
        (meeting,) = LibraryScanner(root).scan().meetings
        assert meeting.audio_file.name == capture.publication.name
        assert len(list(root.glob("*.opus"))) == 1
    finally:
        release.set()
        if not capture.finished:
            capture.stop(reason="appQuit")
            assert capture.stopped.wait(5)


def test_quit_cancels_native_publish_queued_behind_another_capture(tmp_path, monkeypatch):
    from concurrent.futures import CancelledError

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    entered, release = threading.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def occupied():
        entered.set()
        # Released after the test saves a capture durably.
        assert release.wait(DURABLE_BOUND)

    executor.submit(occupied)
    assert entered.wait(2)
    monkeypatch.setattr(library_publication, "_ENCODER", executor)
    recorder = NativeRecorder()
    finished = []
    recorder.connect("finished", lambda _recorder, path: finished.append(path))
    try:
        recorder.start(test=True)
        spin_until(lambda: recording(recorder))
        sid = recorder.session.manifest["id"]
        recorder.stop()
        spin_until(lambda: recorder._publication_future is not None)
        assert capture_busy(sid)
        recorder.cancel_publication()
        spin_until(lambda: bool(finished), timeout=2)
        assert not release.is_set() and not capture_busy(sid)
        assert recorder.session.manifest["state"] == "completed"
        assert not list((tmp_path / "Library").glob("*.opus"))
        with pytest.raises(CancelledError):
            recorder._publication_future.result()
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        if recorder.pipeline is not None and not recorder.stopping:
            recorder.stop()
        spin_until(lambda: not recorder.stopping)
        recorder.close()


def test_browser_rpc_stop_acknowledges_pcm_and_returns_saving_paths_for_quit(tmp_path, monkeypatch):
    from test_browser_capture import start as start_wire

    from cassini_talk.browser_audio_proxy import BrowserAudioProxy
    from cassini_talk.browser_service import BrowserService, request
    from cassini_talk.capture_activity import read_browser_activity

    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    entered, release, ready = threading.Event(), threading.Event(), threading.Event()
    original = finalize.finalize

    def slow_publish(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", slow_publish)
    service = BrowserService(tmp_path / "broker-config.json", tap_factory=Tap, notifier=lambda *_args: None)
    socket = tmp_path / "broker/service.sock"
    worker = threading.Thread(target=service.serve, args=(socket, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    client = BrowserAudioProxy(path=socket)
    try:
        path = start_wire(client, "rpc-published", service=service)["path"]
        remote(client, "remote")
        stopped = request("stop-all", path=socket)
        assert entered.wait(3)
        assert not any(state.get("recording") for state in stopped["statuses"])
        assert any(state.get("stopping") for state in stopped["statuses"])
        assert stopped["stoppedPaths"] == [path]
        capture = next(iter(service.captures.values()))
        sid = capture.host.session.manifest["id"]
        assert read_browser_activity(socket) == {sid: "saving"}
        # An app quitting after capture has stopped still transfers its queued
        # write-down intent using the Saving path, without waiting for encode.
        quitting = request("stop-all", path=socket)
        assert quitting["stoppedPaths"] == [path]
        release.set()
        saved(capture)
    finally:
        release.set()
        client.close()
        service.closed.set()
        worker.join(3)
        assert not worker.is_alive()


def test_native_publication_failure_is_distinct_from_a_capture_start_failure(tmp_path, monkeypatch):
    import errno

    from cassini_talk.publication_state import failure_notice

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    failure = OSError(errno.ENOSPC, "Synthetic full disk after Stop")

    def full(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(finalize, "finalize", full)
    recorder = NativeRecorder()
    finished = []
    recorder.connect("finished", lambda *_: finished.append(True))
    try:
        recorder.start(test=True)
        spin_until(lambda: recording(recorder))
        recorder.stop()
        spin_until(lambda: bool(finished))
        assert recorder.publication_error is failure
        assert failure_notice(recorder.publication_error) == "library.publishDiskLow"
        assert not list((tmp_path / "Library").glob("*.opus"))
        assert all((recorder.session.directory / t["file"]).is_file() for t in recorder.session.manifest["tracks"])
        assert not capture_busy(recorder.session.manifest["id"])
    finally:
        recorder.close()


def test_browser_stop_queued_during_opus_publication_starts_transcription_when_published(tmp_path, monkeypatch):
    from durable import Finished
    from test_browser_capture import start as start_wire

    from cassini_talk.browser_audio_proxy import BrowserAudioProxy
    from cassini_talk.browser_service import BrowserService, request
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.transcription import TranscriptionQueue

    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    entered, release, ready = threading.Event(), threading.Event(), threading.Event()
    original = finalize.finalize

    def slow_publish(*args, **kwargs):
        entered.set()
        assert release.wait(DURABLE_BOUND)
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", slow_publish)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(dict(type="planned", etaMinutes=0, segments=1))
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=300, text="Let us review the quarterly budget today")],
                )
            )
            return dict(diagnostics=[])

    finished = Finished()
    queue = TranscriptionQueue(lambda _d: None, finished, root=root, bundle=tmp_path, client_factory=Client)
    service = BrowserService(tmp_path / "broker-config.json", tap_factory=Tap, notifier=lambda *_args: None)
    socket = tmp_path / "broker/service.sock"
    worker = threading.Thread(target=service.serve, args=(socket, ready), daemon=True)
    worker.start()
    assert ready.wait(2)
    client = BrowserAudioProxy(path=socket)
    try:
        path = start_wire(client, "teams-stop-queued", service=service)["path"]
        remote(client, "remote")
        stopped = request("stop-all", path=socket)
        assert entered.wait(3)
        assert stopped["stoppedPaths"] == [path]
        assert not list(root.glob("*.opus"))

        # Stop acknowledged PCM before .opus muxing finished: write_down records durable
        # queued intent and waits for publication.
        assert queue.enqueue(path, resume=True)
        finished.wait(1, exactly=True)
        assert read_metadata(path)["transcript"]["status"] == "queued"

        # Now .opus muxing finishes and the broker reports the saved recording.
        release.set()
        capture = next(iter(service.captures.values()))
        saved(capture)
        snapshot = request(path=socket)
        assert any(r.get("path") == path for r in snapshot["recordings"])

        # Resuming write_down on the published recording runs inference immediately.
        assert queue.enqueue(path, resume=True)
        finished.wait(2, exactly=True)

        (meeting,) = LibraryScanner(root).scan().meetings
        assert meeting.published_file is not None and meeting.published_file.exists()
        assert meeting.transcript == "done"
        assert [w.text for w in meeting.words] == ["Let us review the quarterly budget today"]
    finally:
        release.set()
        queue.close()
        client.close()
        service.closed.set()
        worker.join(3)
        assert not worker.is_alive()


def test_real_publication_finishing_after_prepared_checks_resume_does_not_strand_browser_recording(
    tmp_path, monkeypatch
):
    from durable import Finished, durable
    from test_library import capture

    from cassini_talk.library_publication import capture_parent, publish_stopped
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.transcription import TranscriptionQueue

    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    session = capture(capture_parent(root))
    lock_released, allow_after_release = threading.Event(), threading.Event()
    finished = Finished()

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            notify(dict(type="planned", etaMinutes=0, segments=1))
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=300, text="Let us review the quarterly budget today")],
                )
            )
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _d: None, finished, root=root, bundle=tmp_path, client_factory=Client)
    real_lock = queue.lock
    original_prepared = queue._prepared
    arm_gate = [True]
    watch_exit = [False]

    class GateLock:
        def acquire(self, *args, **kwargs):
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            return real_lock.__enter__()

        def __exit__(self, *exc):
            result = real_lock.__exit__(*exc)
            if watch_exit[0]:
                watch_exit[0] = False
                lock_released.set()
                durable(allow_after_release, "the test did not release _prepared after lock exit")
            return result

    def gated_prepared(directory, future):
        if arm_gate[0]:
            arm_gate[0] = False
            watch_exit[0] = True
        return original_prepared(directory, future)

    queue.lock = GateLock()
    queue._prepared = gated_prepared
    try:
        # 1. Stop delivers while .opus muxing is still running: durable queued intent is recorded,
        # and _mark_queued returns None because published_file is still None.
        accepted = int(queue.enqueue(session.directory, resume=True))
        assert accepted == 1
        durable(lock_released, "_prepared did not reach its lock release")
        assert read_metadata(session.directory)["transcript"]["status"] == "queued"

        # 2. Right after _prepared checks _resume_pending and releases queue.lock, real .opus
        # publication finishes and triggers enqueue(resume=True) before _prepared finishes retiring.
        published = publish_stopped(session)
        assert published is not None and (root / published.name).is_file()
        resumed = queue.enqueue(session.directory, resume=True)
        assert resumed
        accepted += int(resumed)
        allow_after_release.set()

        finished.wait(accepted, exactly=True)
        (meeting,) = LibraryScanner(root).scan().meetings
        assert meeting.published_file == root / published.name
        assert meeting.transcript == "done"
        assert [w.text for w in meeting.words] == ["Let us review the quarterly budget today"]
    finally:
        allow_after_release.set()
        queue.close()
