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
