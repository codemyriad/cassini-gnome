# SPDX-License-Identifier: GPL-3.0-or-later
"""Fresh direct-open facts must join the cache used by activity reprojection."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("GTK/Adwaita window integration runs in distro jobs", allow_module_level=True)
from cassini_talk.library import LibrarySnapshot, Meeting  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402


def meeting(directory, title, instant):
    return Meeting(id=directory.name, directory=directory, title=title, started_at_ms=instant, duration_ms=1000)


@pytest.mark.parametrize("already_scanned", [True, False])
def test_fresh_open_updates_raw_cache_without_discarding_other_library_facts(tmp_path, already_scanned):
    old = meeting(tmp_path / "Recording-a", "Old editable title", 1000)
    unrelated = meeting(tmp_path / "Recording-b", "Another meeting", 2000)
    raw = LibrarySnapshot(
        meetings=(unrelated, old) if already_scanned else (unrelated,),
        directories=(unrelated.directory, old.directory) if already_scanned else (unrelated.directory,),
        diagnostics=("retained scan diagnostic",),
    )
    observed = []
    window = SimpleNamespace(_source_snapshot=raw, snapshot=raw, _updated=observed.append)
    fresh = replace(old, title="New direct-open title", metadata_editable=False)
    LibraryWindow._library_loaded(window, fresh)
    assert window._source_snapshot is observed[0]
    assert window._source_snapshot.meetings == (unrelated, fresh)
    assert window._source_snapshot.meetings[1] is fresh
    assert set(window._source_snapshot.directories) == {old.directory, unrelated.directory}
    assert window._source_snapshot.diagnostics == raw.diagnostics
    assert raw.meetings == ((unrelated, old) if already_scanned else (unrelated,))


def test_direct_open_before_first_scan_does_not_need_an_existing_cache(tmp_path):
    fresh = meeting(tmp_path / "Recording-new", "Opened before scan", 1000)
    window = SimpleNamespace(_source_snapshot=None, snapshot=None, _updated=lambda _snapshot: None)
    LibraryWindow._library_loaded(window, fresh)
    assert window._source_snapshot.meetings == (fresh,)
    assert window._source_snapshot.directories == (fresh.directory,)


@pytest.mark.parametrize("already_scanned", [True, False])
@pytest.mark.parametrize("collection", [False, True])
def test_real_direct_open_survives_an_unrelated_incremental_monitor_scan(
    tmp_path, monkeypatch, already_scanned, collection
):
    import json

    from gi.repository import Gio, GLib
    from test_library import capture, write_metadata

    from cassini_talk.library_monitor import LibraryMonitor
    from cassini_talk.meeting_store import atomic_json

    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target, other = capture(root), capture(root)
    write_metadata(target.directory, title="Old title", titleSource="user")
    activity = {target.manifest["id"]: "saving"}
    monitor = LibraryMonitor(
        lambda _snapshot: None, None if collection else root, capture_activity=lambda: activity, debounce_ms=0
    )
    original = monitor.scanner.scan(changed_directories=None if already_scanned else {other.directory})
    delivered, opened, increments = [], [], []
    window = SimpleNamespace(
        monitor=monitor, _source_snapshot=original, snapshot=original, _opening_files=0, meeting_page=None
    )
    window.get_application = lambda: SimpleNamespace(meeting_trash=SimpleNamespace(scanned=lambda *_: None))

    def updated(snapshot):
        window.snapshot = snapshot
        delivered.append(snapshot)

    window._updated = updated
    window.open_meeting = lambda model, **_kwargs: opened.append(model)
    window.toast = lambda message: pytest.fail(message)
    for name in ("_library_loaded", "_library_scanned", "_opened_file"):
        setattr(window, name, getattr(LibraryWindow, name).__get__(window))
    monitor.callback = window._library_scanned

    def dispatch_until(predicate):
        expired = []
        timer = GLib.timeout_add_seconds(5, lambda: expired.append(True) or False)
        try:
            while not predicate():
                assert not expired, "the actual monitor/open completion did not arrive"
                GLib.MainContext.default().iteration(True)
        finally:
            if not expired:
                GLib.source_remove(timer)

    try:
        # Real disk observations, including the reader's future-version
        # read-only rule; no in-memory replacement or injected Future.
        metadata = json.loads((target.directory / "meeting.json").read_text())
        atomic_json(target.directory / "meeting.json", dict(metadata, version=2))
        manifest = json.loads((target.directory / "session.json").read_text())
        atomic_json(target.directory / "session.json", dict(manifest, title="Fresh disk title"))
        LibraryWindow.open_file(window, Gio.File.new_for_path(str(target.directory / "session.json")))
        dispatch_until(lambda: window._opening_files == 0)
        assert len(opened) == 1 and opened[0].title == "Fresh disk title"
        assert not opened[0].metadata_editable
        dispatch_until(
            lambda: (
                monitor._future is None
                and not monitor._timer
                and not monitor._pending_dirs
                and not monitor._pending_full
            )
        )
        scan = monitor.scanner.scan

        def observed(**kwargs):
            snapshot = scan(**kwargs)
            if kwargs.get("changed_directories") == {other.directory}:
                increments.append(snapshot)
            return snapshot

        monkeypatch.setattr(monitor.scanner, "scan", observed)
        monitor.refresh({other.directory})
        dispatch_until(lambda: increments and monitor._future is None)
        (retained,) = [row for row in increments[0].meetings if row.id == target.manifest["id"]]
        assert retained.title == "Fresh disk title" and not retained.metadata_editable
        assert retained.lifecycle == "saving", "the direct load must use current capture activity"
        assert target.directory in monitor._directories
        assert next(row for row in delivered[-1].meetings if row.id == retained.id).title == retained.title
    finally:
        monitor.close()
