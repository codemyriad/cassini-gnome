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
        value = dict(version=1, id=session.manifest["id"], revision=0, title="Atomic app title")
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
