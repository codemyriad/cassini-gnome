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


def test_stop_updates_live_row_in_place_without_rebuilding_unrelated_5000_rows(tmp_path):
    """Pressing Stop on a 5,001-meeting Library must update only the stopped
    meeting's row in place and retain all 5,000 cached unrelated rows."""
    from datetime import UTC

    class FakeWidget:
        def __init__(self, visible=True):
            self.visible = visible
            self.child = None
            self.children = []
            self.selected_row = None

        def set_visible(self, v):
            self.visible = v

        def set_visible_child_name(self, name):
            self.visible_name = name

        def get_visible_child_name(self):
            return getattr(self, "visible_name", "library")

        def set_spinning(self, _v):
            pass

        def handler_block(self, _h):
            pass

        def handler_unblock(self, _h):
            pass

        def remove_all(self):
            self.children.clear()

        def append(self, item):
            self.children.append(item)

        def select_row(self, row):
            self.selected_row = row

        def get_row_at_index(self, idx):
            return self.children[idx]

        def get_selected_row(self):
            return self.selected_row

        def set_child(self, c):
            self.child = c

        def get_child(self):
            return self.child

    live_id = "00000000-0000-4000-8000-000000005001"
    live = Meeting(
        id=live_id,
        directory=tmp_path / f"Recording-{live_id}",
        title="Live browser call",
        started_at_ms=1_793_107_000_000,
        duration_ms=42_000,
        lifecycle="recording",
        capture_source="chromium",
        has_audio=True,
    )
    saved = tuple(
        Meeting(
            id=f"00000000-0000-4000-8000-{i:012d}",
            directory=tmp_path / f"Recording-00000000-0000-4000-8000-{i:012d}",
            title=f"Saved meeting {i}",
            started_at_ms=1_793_106_000_000 - i * 60_000,
            duration_ms=600_000,
            lifecycle="saved",
            has_audio=True,
            quote_line="“Synthetic quote.”",
        )
        for i in range(5000)
    )
    source_snapshot = LibrarySnapshot(meetings=(live, *saved))

    app = SimpleNamespace(
        library_recovery=None,
        meeting_trash=SimpleNamespace(project=lambda snap: snap, hides=lambda _d: False, allowed=lambda *_: False),
        observe_library=lambda *_args: None,
        comfort=SimpleNamespace(elapsed=lambda _m: 42_000),
        _quitting=False,
        library_relocator=None,
    )
    row_builds, row_fills = [], []
    window = SimpleNamespace(
        get_application=lambda: app,
        now_ms=1_793_107_080_000,
        tz=UTC,
        _fixed_now=None,
        _activity={live_id: "recording"},
        _source_snapshot=source_snapshot,
        snapshot=None,
        _observed_phases=None,
        _loaded_key=None,
        _loaded_directory=None,
        _external_meeting=False,
        search_page=None,
        recording_page=None,
        meeting_page=None,
        export_dialog=None,
        zip_export_dialog=None,
        rename_dialog=None,
        _pending_recording=None,
        _pending_selection=None,
        _opening_files=0,
        _list_signature=None,
        _row_by_key={},
        _rows_layout=None,
        _group_layout_key=None,
        _rows_tz=None,
        _live_badges={},
        selected=None,
        _selection_handler=1,
        _focused_editors=set(),
        privacy_footer=FakeWidget(),
        received_hint=FakeWidget(),
        root_box=FakeWidget(),
        rows=FakeWidget(),
        loading=FakeWidget(),
        open_pending_recording=lambda: False,
        lookup_action=lambda _name: None,
        _update_live=lambda: None,
        _selected=lambda _rows, row: setattr(window, "selected", getattr(row, "meeting", None)),
    )
    for name in ("_project_activity", "_refresh_export", "_row_signature", "_updated"):
        setattr(window, name, getattr(LibraryWindow, name).__get__(window))

    def fake_fill(row, m, sig=None):
        row_fills.append(m.key)
        row.meeting = m
        row._row_signature = window._row_signature(m) if sig is None else sig
        row.set_child((m.key, m.lifecycle))

    def fake_row(m):
        row_builds.append(m.key)
        r = FakeWidget()
        fake_fill(r, m)
        return r

    window._day_heading = lambda _title: FakeWidget()
    window._fill_row = fake_fill
    window._row = fake_row

    # Initial load builds 5,001 rows once:
    window._updated(source_snapshot)
    assert len(row_builds) == 5001
    unrelated_rows = [window._row_by_key[m.key] for m in saved]
    unrelated_children = [r.get_child() for r in unrelated_rows]
    live_row = window._row_by_key[live.key]

    # Pressing Stop transitions activity to 'saving' — zero unrelated rows may rebuild:
    row_builds.clear()
    row_fills.clear()
    window._activity = {live_id: "saving"}
    window._updated(source_snapshot)

    assert row_builds == [], "Stop must never rebuild cached rows via _row()"
    assert row_fills == [live.key], "Stop must update only the stopped meeting's row"
    assert window._row_by_key[live.key] is live_row
    assert live_row.meeting.lifecycle == "saving"
    assert [window._row_by_key[m.key] for m in saved] == unrelated_rows
    assert [r.get_child() for r in unrelated_rows] == unrelated_children

    # Subsequent fresh scan with identical 5,000 unrelated rows also retains every cached row:
    row_fills.clear()
    fresh_saved = tuple(replace(m) for m in saved)
    window._updated(LibrarySnapshot(meetings=(replace(live, lifecycle="saved"), *fresh_saved)))
    assert row_builds == []
    assert row_fills == [live.key]
    assert [window._row_by_key[m.key] for m in saved] == unrelated_rows
    assert [r.get_child() for r in unrelated_rows] == unrelated_children
