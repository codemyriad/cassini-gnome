# SPDX-License-Identifier: GPL-3.0-or-later
"""GFileMonitor only watches temporary test directories; no GTK/display/audio."""

import json
import time

import pytest

gi = pytest.importorskip("gi")
from gi.repository import Gio, GLib  # noqa: E402

from cassini_viewer.library_monitor import LibraryMonitor  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402


def spin_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Monitor did not deliver the changed Library"


def test_monitor_handles_new_root_atomic_metadata_and_capture_activity(tmp_path):
    root = tmp_path / "Music/Cassini"
    snapshots = []
    activity = {}
    monitor = LibraryMonitor(snapshots.append, root, capture_activity=lambda: activity, debounce_ms=10)
    session = None
    try:
        monitor.refresh()
        spin_until(lambda: bool(snapshots))
        assert not snapshots[-1].meetings and not root.exists()
        session = RecordingSession(root, "Initial title")
        session.add_track("one", None, "remote", 48000, 1)
        session.append("one", bytes(9600), 50)
        session.checkpoint()
        spin_until(lambda: snapshots[-1].meetings)
        assert snapshots[-1].meetings[0].lifecycle == "saved"
        activity[session.manifest["id"]] = "recording"
        monitor.refresh()
        spin_until(lambda: snapshots[-1].meetings[0].lifecycle == "recording")
        value = dict(
            version=1,
            id=session.manifest["id"],
            revision=0,
            title="Atomic app title",
            titleSource="user",
            notes=[],
            speakers=[],
            transcript=dict(status="none", reference=None),
            etaMinutes=None,
            error=None,
            quoteOverride=None,
            received=None,
        )
        temporary = session.directory / ".meeting-test.tmp"
        temporary.write_text(json.dumps(value))
        temporary.replace(session.directory / "meeting.json")
        spin_until(lambda: snapshots[-1].meetings[0].title == "Atomic app title")
        activity.clear()
        session.finish()
        spin_until(
            lambda: (
                snapshots[-1].meetings[0].lifecycle == "saved" and snapshots[-1].meetings[0].interrupted_at_ms is None
            )
        )
    finally:
        monitor.close()
        if session:
            session.finish()


def test_monitor_stops_delivering_after_close(tmp_path):
    snapshots = []
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=10)
    monitor.refresh()
    monitor.close()
    session = RecordingSession(tmp_path, "No callback after close")
    session.finish()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.1
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)
    assert snapshots == []


def test_failed_watch_does_not_prevent_scan_or_explicit_retry(tmp_path, monkeypatch):
    snapshots = []

    def denied(*_args):
        raise GLib.Error("synthetic watch permission denied")

    monkeypatch.setattr(Gio.File, "monitor_directory", denied)
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=10)
    try:
        monitor.refresh()
        spin_until(lambda: bool(snapshots))
        assert not snapshots[-1].meetings
        session = RecordingSession(tmp_path, "Manual refresh still works")
        session.finish()
        monitor.refresh()
        spin_until(lambda: bool(snapshots[-1].meetings))
        assert snapshots[-1].meetings[0].title == "Manual refresh still works"
    finally:
        monitor.close()


def test_worker_keeps_main_context_responsive_and_delivers_on_main_thread(tmp_path, monkeypatch):
    import threading

    snapshots, threads = [], []
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    def delivered(snapshot):
        assert threading.get_ident() == main_thread
        snapshots.append(snapshot)

    monitor = LibraryMonitor(delivered, tmp_path, debounce_ms=1)
    original = monitor.scanner.scan

    def blocked_scan(**kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "Test did not release worker"
        return original(**kwargs)

    monkeypatch.setattr(monitor.scanner, "scan", blocked_scan)
    try:
        monitor.refresh()
        spin_until(entered.is_set)
        heartbeat = []
        GLib.idle_add(lambda: heartbeat.append(True) and False)
        spin_until(lambda: bool(heartbeat))
        assert not snapshots and threads == [threads[0]] and threads[0] != main_thread
        release.set()
        spin_until(lambda: bool(snapshots))
    finally:
        release.set()
        monitor.close()


def test_metadata_event_is_incremental_monitors_are_reused_and_wav_events_are_ignored(tmp_path, monkeypatch):
    from test_library import capture, write_metadata

    sessions = [capture(tmp_path), capture(tmp_path)]
    snapshots, scans = [], []
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=10)
    original = monitor.scanner.scan

    def scanned(**kwargs):
        scans.append(kwargs["changed_directories"])
        return original(**kwargs)

    monkeypatch.setattr(monitor.scanner, "scan", scanned)
    try:
        monitor.refresh()
        spin_until(lambda: bool(snapshots))
        watches = dict(monitor._monitors)
        target = sessions[0]
        write_metadata(target.directory, title="Only this row changed", titleSource="user")
        spin_until(lambda: any(m.title == "Only this row changed" for m in snapshots[-1].meetings))
        assert scans[-1] == {target.directory}
        assert all(monitor._monitors[path] is value for path, value in watches.items())
        wav = Gio.File.new_for_path(str(target.directory / target.manifest["tracks"][0]["file"]))
        for event in (
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
        ):
            monitor._changed(None, wav, None, event)
        assert monitor._timer == 0, "WAV events must not schedule scans"
    finally:
        monitor.close()


def test_unpublished_received_folder_is_watched_until_metadata_is_published(tmp_path):
    from test_library import received

    directory = received(tmp_path)
    metadata = directory / "meeting.json"
    content = metadata.read_bytes()
    metadata.unlink()
    snapshots = []
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=10)
    try:
        monitor.refresh()
        spin_until(lambda: bool(snapshots))
        assert not snapshots[-1].meetings and directory in monitor._monitors
        metadata.write_bytes(content)
        spin_until(lambda: bool(snapshots[-1].meetings))
        assert snapshots[-1].meetings[0].origin == "received"
    finally:
        monitor.close()


def test_trailing_debounce_resets_and_caps_wait_at_two_seconds(tmp_path, monkeypatch):
    monitor = LibraryMonitor(lambda _snapshot: None, tmp_path)
    clock, delays, removed = [0], [], []

    def scheduled(delay, _callback):
        delays.append(delay)
        return len(delays)

    monkeypatch.setattr(GLib, "get_monotonic_time", lambda: clock[0] * 1000)
    monkeypatch.setattr(GLib, "timeout_add", scheduled)
    monkeypatch.setattr(GLib, "source_remove", removed.append)
    try:
        for at in (0, 100, 1950, 2100):
            clock[0] = at
            monitor.refresh()
        assert delays == [250, 250, 50, 0]
        assert removed == [1, 2, 3]
    finally:
        monitor.close()


def test_close_during_worker_scan_suppresses_late_delivery(tmp_path, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()
    snapshots = []
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=1)
    original = monitor.scanner.scan

    def scan(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)

    monkeypatch.setattr(monitor.scanner, "scan", scan)
    try:
        monitor.refresh()
        spin_until(entered.is_set)
        monitor.close()
        release.set()
        spin_until(lambda: monitor._future is None)
        assert not snapshots
    finally:
        release.set()
        monitor.close()


def test_watch_target_resolution_and_stats_happen_in_worker(tmp_path, monkeypatch):
    import threading
    from pathlib import Path

    main = threading.get_ident()
    original_resolve, original_is_dir = Path.resolve, Path.is_dir

    def resolved(path, *args, **kwargs):
        assert threading.get_ident() != main, "Library path resolution blocked the UI"
        return original_resolve(path, *args, **kwargs)

    def is_dir(path):
        assert threading.get_ident() != main, "Library ancestor stat blocked the UI"
        return original_is_dir(path)

    snapshots = []
    monitor = LibraryMonitor(snapshots.append, tmp_path / "absent", debounce_ms=1)
    monkeypatch.setattr(Path, "resolve", resolved)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    try:
        monitor.refresh()
        spin_until(lambda: bool(snapshots))
        monitor._changed(None, Gio.File.new_for_path(str(tmp_path / "unrelated")), None, Gio.FileMonitorEvent.CHANGED)
    finally:
        monitor.close()


def test_startup_metadata_change_after_read_is_observed(tmp_path, monkeypatch):
    from test_library import capture, write_metadata

    session = capture(tmp_path)
    snapshots = []
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=1)
    original = monitor.scanner.scan
    first = True

    def scan(**kwargs):
        nonlocal first
        result = original(**kwargs)
        if first:
            first = False
            # A real filesystem event after the initial snapshot was read,
            # before any completion callback can install watches.
            write_metadata(session.directory, title="Changed during bootstrap", titleSource="user")
        return result

    monkeypatch.setattr(monitor.scanner, "scan", scan)
    try:
        monitor.refresh()
        spin_until(lambda: snapshots and snapshots[-1].meetings[0].title == "Changed during bootstrap")
    finally:
        monitor.close()


def test_first_scan_failure_retains_watches_and_recovers_on_metadata_event(tmp_path, monkeypatch):
    from test_library import capture, write_metadata

    session = capture(tmp_path)
    snapshots = []
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=1)
    original = monitor.scanner.scan
    first = True

    def scan(**kwargs):
        nonlocal first
        if first:
            first = False
            raise RuntimeError("synthetic initial scan failure")
        return original(**kwargs)

    monkeypatch.setattr(monitor.scanner, "scan", scan)
    try:
        monitor.refresh()
        spin_until(lambda: bool(snapshots))
        assert snapshots[-1].unavailable and session.directory in monitor._monitors
        write_metadata(session.directory, title="Recovered", titleSource="user")
        spin_until(lambda: snapshots[-1].meetings and snapshots[-1].meetings[0].title == "Recovered")
    finally:
        monitor.close()


def test_preview_grouping_runs_on_worker_without_blocking_main_loop(tmp_path, monkeypatch):
    import threading

    from test_library import capture, write_metadata

    from cassini_viewer import library

    session = capture(tmp_path)
    (session.directory / "transcript.json").write_text(
        json.dumps(dict(language="en", items=[dict(speaker="a", startMs=0, endMs=100, text="A real preview")]))
    )
    write_metadata(
        session.directory,
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
    )
    entered, release = threading.Event(), threading.Event()
    main = threading.get_ident()
    original = library.build_turns

    def group(words):
        assert threading.get_ident() != main
        entered.set()
        assert release.wait(3)
        return original(words)

    monkeypatch.setattr(library, "build_turns", group)
    snapshots = []
    monitor = LibraryMonitor(snapshots.append, tmp_path, debounce_ms=1)
    try:
        monitor.refresh()
        spin_until(entered.is_set)
        heartbeats = []
        GLib.idle_add(lambda: heartbeats.append(True) and False)
        spin_until(lambda: bool(heartbeats))
        assert not snapshots
        release.set()
        spin_until(lambda: bool(snapshots))
        assert snapshots[-1].meetings[0].preview_turns[0].text == "A real preview"
    finally:
        release.set()
        monitor.close()
