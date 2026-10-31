# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""Private startup worker: live Saving, cancellation, no inference before publish."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication tests require GStreamer", allow_module_level=True)

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


def test_file_only_write_down_recreates_only_optional_working_cache(tmp_path, monkeypatch):
    import shutil

    from cassini_viewer import portable

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid)
    shutil.rmtree(directory)
    completed = threading.Event()
    seen = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, progress):
            seen.extend(tracks)
            assert len(tracks) == 1 and tracks[0]["path"] == str(tmp_path / result.name)
            progress(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=90, text="From the file")],
                )
            )
            return dict(diagnostics=[])

    queue = TranscriptionQueue(
        lambda _directory: None,
        lambda _directory: completed.set(),
        root=tmp_path,
        bundle=tmp_path,
        client_factory=Client,
    )
    try:
        assert queue.enqueue(directory)
        assert completed.wait(5)
        assert seen
        (meeting,) = LibraryScanner(tmp_path).scan().meetings
        assert meeting.transcript == "done" and meeting.words[0].text == "From the file"
        assert portable.load_words(portable.read_meeting(str(meeting.audio_file)), "words")[0].text == "From the file"
        assert (directory / "transcript.json").is_file()
        assert not (directory / "session.json").exists() and not list(directory.glob("*.wav"))
        assert list(library_publication.recover_completed(tmp_path)) == [], "Published caches need no capture manifest"
    finally:
        queue.close()


def wait_recovery(condition, description):
    from gi.repository import GLib

    loop = GLib.MainLoop()
    failures = []

    def observe():
        try:
            done = condition()
        except BaseException as error:
            failures.append(error)
            done = True
        if done:
            loop.quit()
        return not done

    def expired():
        failures.append(AssertionError(f"Timed out waiting for {description}"))
        loop.quit()
        return False

    tick, limit = GLib.timeout_add(10, observe), GLib.timeout_add_seconds(5, expired)
    loop.run()
    for source in (tick, limit):
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
    if failures:
        raise failures[0]


def test_publication_retry_recovers_retained_audio_without_an_app_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(tmp_path, 0.1)
    original = finalize.finalize
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("Synthetic disk became unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", fail_once)
    app = App()
    recovery = LibraryRecovery(app)
    try:
        wait_recovery(lambda: recovery.future is None and bool(recovery.failures), "first publication failure")
        assert app.holds == 0 and directory.exists()
        assert recovery.retry(directory)
        wait_recovery(lambda: recovery.future is None and app.holds == 0, "publication retry completion")
        assert not recovery.failures and len(attempts) == 2
        (meeting,) = LibraryScanner(tmp_path).scan().meetings
        assert meeting.id == sid and meeting.published_file
    finally:
        recovery.close()


def test_retry_requested_during_a_sweep_is_not_lost_when_the_older_attempt_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    _sid, directory = simple_session(tmp_path, 0.1)
    entered, release = threading.Event(), threading.Event()
    original = finalize.finalize
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5), "Test did not release the first publication attempt"
            raise OSError("Synthetic delayed publication failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", fail_once)
    app = App()
    recovery = LibraryRecovery(app)
    try:
        wait_recovery(entered.is_set, "first publication attempt enters encoder")
        assert recovery.retry(directory)
        release.set()
        wait_recovery(
            lambda: len(attempts) == 2 and recovery.future is None and not recovery.timer,
            "the newer retry revision runs after the older sweep",
        )
        assert not recovery.failures and app.holds == 0
        assert LibraryScanner(tmp_path).scan().meetings[0].published_file
    finally:
        release.set()
        recovery.close()


@pytest.mark.parametrize(
    "state,ended,frames,eligible",
    [
        ("interrupted", False, 4800, False),
        ("interrupted", True, 4800, True),
        ("completed", False, 4800, True),
        ("completed", False, 0, False),
        ("interrupted", True, 0, False),
    ],
)
def test_scans_do_not_restart_sweeps_for_unstopped_or_empty_sessions(tmp_path, state, ended, frames, eligible):
    from types import SimpleNamespace

    from cassini_viewer.library import LibrarySnapshot, Meeting

    directory = tmp_path / ".cassini/sessions/Recording-12345678-1234-1234-1234-123456789abc"
    meeting = Meeting(
        id=directory.name.removeprefix("Recording-"),
        directory=directory,
        title="Retained",
        started_at_ms=None,
        duration_ms=frames // 48,
        capture_state=state,
        capture_events=([dict(type="end", atMs=100, reason="user")] if ended else []),
        tracks=(SimpleNamespace(frames=frames),),
    )
    recovery = object.__new__(LibraryRecovery)
    recovery.cancelled = threading.Event()
    recovery.future, recovery.timer, recovery.attempted = None, 0, set()
    calls = []
    recovery.start = lambda: calls.append(True)
    for _ in range(3):
        recovery.observe(LibrarySnapshot(meetings=(meeting,)))
    assert len(calls) == (3 if eligible else 0)


def test_retry_does_not_read_routing_or_resolve_paths_on_gtk(tmp_path, monkeypatch):
    from pathlib import Path

    from cassini_viewer import library_location

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    _sid, directory = simple_session(tmp_path, 0.1)
    app = App()
    recovery = LibraryRecovery(app)
    try:
        wait_recovery(lambda: recovery.future is None, "initial publication sweep")

        def on_worker(operation):
            def checked(*args, **kwargs):
                assert threading.get_ident() != app.thread, "Retry performed Library IO on GTK"
                return operation(*args, **kwargs)

            return checked

        for name in ("owned_directory", "roots"):
            monkeypatch.setattr(library_location, name, on_worker(getattr(library_location, name)))
        monkeypatch.setattr(Path, "resolve", on_worker(Path.resolve))
        assert recovery.retry(directory)
        wait_recovery(lambda: recovery.future is None and not recovery.timer, "worker-only publication retry")
        assert not recovery.failures and app.holds == 0
    finally:
        recovery.close()


def test_retry_resolves_a_moved_dialog_path_on_the_worker(tmp_path, monkeypatch):
    from cassini_viewer import library_location

    root = tmp_path / "new"
    root.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    _sid, directory = simple_session(root, 0.1)
    original = finalize.finalize
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("Initial publication failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize", fail_once)
    app = App()
    recovery = LibraryRecovery(app)
    try:
        wait_recovery(lambda: recovery.future is None and bool(recovery.failures), "initial publication failure")
        old = tmp_path / "old/.cassini/sessions" / directory.name
        value = library_location.load()
        value["aliases"][str(old)] = str(directory)
        library_location.save(value)
        assert library_location.owned_directory(old) == directory
        assert recovery.retry(old), "A dialog can retain its pre-Move path"
        wait_recovery(lambda: recovery.future is None and not recovery.timer, "routed publication retry")
        assert len(attempts) == 2 and not recovery.failures
        assert LibraryScanner(root).scan().meetings[0].published_file
    finally:
        recovery.close()
