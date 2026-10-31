# SPDX-License-Identifier: GPL-3.0-or-later
"""Real synthetic capture owners publish after Stop, never on the GTK thread."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_browser_capture import Tap, remote
from test_recorder import spin_until

from cassini_viewer import library_publication
from cassini_viewer.browser_capture import BrowserCapture
from cassini_viewer.library import LibraryScanner
from cassini_viewer.library_location import capture_busy
from cassini_viewer.library_v2 import finalize
from cassini_viewer.native_recorder import NativeRecorder


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
        spin_until(lambda: recorder.session and all(t["frames"] >= 4800 for t in recorder.session.manifest["tracks"]))
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
        assert capture.stopped.wait(5)
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
        assert release.wait(5)

    executor.submit(occupied)
    assert entered.wait(2)
    monkeypatch.setattr(library_publication, "_ENCODER", executor)
    recorder = NativeRecorder()
    finished = []
    recorder.connect("finished", lambda _recorder, path: finished.append(path))
    try:
        recorder.start(test=True)
        spin_until(lambda: recorder.session and all(t["frames"] >= 4800 for t in recorder.session.manifest["tracks"]))
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
