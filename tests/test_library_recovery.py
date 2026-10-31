# SPDX-License-Identifier: GPL-3.0-or-later
"""Private startup worker: live Saving, cancellation, no inference before publish."""

import threading
from concurrent.futures import ThreadPoolExecutor

from library_v2_support import simple_session
from test_recorder import spin_until

from cassini_viewer import library_publication
from cassini_viewer.library import LibraryScanner
from cassini_viewer.library_location import capture_busy
from cassini_viewer.library_recovery import LibraryRecovery
from cassini_viewer.library_v2 import finalize
from cassini_viewer.meeting_store import read_metadata
from cassini_viewer.transcription import TranscriptionQueue, should_auto_write


class App:
    def __init__(self):
        self.holds = 0
        self.changes = 0
        self.thread = threading.get_ident()

    def hold(self):
        assert threading.get_ident() == self.thread
        self.holds += 1

    def release(self):
        assert threading.get_ident() == self.thread
        self.holds -= 1

    def _transcription_changed(self):
        assert threading.get_ident() == self.thread
        self.changes += 1


def test_startup_publishes_on_worker_with_a_live_saving_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(tmp_path, 0.1)
    entered, release = threading.Event(), threading.Event()
    original = finalize.finalize
    app = App()

    def delayed(*args, **kwargs):
        assert threading.get_ident() != app.thread
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", delayed)
    recovery = LibraryRecovery(app)
    try:
        spin_until(entered.is_set)
        assert app.holds == 1 and recovery.activity == {sid: "saving"}
        assert capture_busy(sid)
        (saving,) = LibraryScanner(tmp_path).scan(capture_activity=recovery.activity).meetings
        assert saving.lifecycle == "saving" and saving.published_file is None
        release.set()
        spin_until(lambda: recovery.future is None and app.holds == 0)
        (saved,) = LibraryScanner(tmp_path).scan().meetings
        assert saved.published_file and not capture_busy(sid)
        assert not recovery.failures and not recovery.activity and app.changes
        assert saved.directory == directory
    finally:
        release.set()
        recovery.close()
        spin_until(lambda: app.holds == 0)


def test_closing_recovery_cancels_a_queued_encode_without_waiting_for_other_meeting(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(tmp_path, 0.1)
    entered, release = threading.Event(), threading.Event()
    encoder = ThreadPoolExecutor(max_workers=1)

    def occupied():
        entered.set()
        assert release.wait(5)

    encoder.submit(occupied)
    assert entered.wait(2)
    monkeypatch.setattr(library_publication, "_ENCODER", encoder)
    app = App()
    recovery = LibraryRecovery(app)
    try:
        spin_until(lambda: bool(recovery.activity))
        assert capture_busy(sid)
        recovery.close()
        spin_until(lambda: app.holds == 0, timeout=2)
        assert not release.is_set() and not capture_busy(sid)
        assert not list(tmp_path.glob("*.opus"))
        assert (directory / "session.json").exists() and not read_metadata(directory).get("published")
    finally:
        release.set()
        encoder.shutdown(wait=True, cancel_futures=True)
        recovery.close()
        spin_until(lambda: app.holds == 0)


def test_transcriber_saves_intent_without_starting_before_publication(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(tmp_path, 0.1)
    complete = threading.Event()

    def no_engine(*_args, **_kwargs):
        raise AssertionError("Recognition started before the Cassini file was published")

    queue = TranscriptionQueue(
        lambda _path: None, lambda _path: complete.set(), root=tmp_path, bundle=tmp_path, client_factory=no_engine
    )
    try:
        assert queue.enqueue(directory)
        assert complete.wait(3)
        assert read_metadata(directory)["transcript"]["status"] == "queued"
        assert not list(tmp_path.glob("*.opus"))
        (before,) = LibraryScanner(tmp_path).scan().meetings
        assert not should_auto_write(before, {})
        finalize.finalize(tmp_path, sid)
        (after,) = LibraryScanner(tmp_path).scan().meetings
        assert after.transcript == "queued" and after.published_file
    finally:
        queue.close()


def test_publication_transition_enables_automatic_writing_once(tmp_path):
    sid, directory = simple_session(tmp_path, 0.1)
    (before,) = LibraryScanner(tmp_path).scan().meetings
    assert not should_auto_write(before, {})
    finalize.finalize(tmp_path, sid)
    (after,) = LibraryScanner(tmp_path).scan().meetings
    assert should_auto_write(after, {before.key: ("saved", "completed", False)})
    assert not should_auto_write(after, {after.key: ("saved", "completed", True)})
    assert not should_auto_write(after, None)  # No transcription of arbitrary historical files.
