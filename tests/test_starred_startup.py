# SPDX-License-Identifier: GPL-3.0-or-later
"""L-34 (D-1040): remembered Starred never auto-selects or lists an unstarred meeting."""

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("GTK/Adwaita window integration runs in distro jobs", allow_module_level=True)
from cassini_talk.library import Meeting  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402


def meeting(tmp_path, name, instant):
    return Meeting(id=name, directory=tmp_path / name, title=name, started_at_ms=instant, duration_ms=1000)


class Rows:
    def __init__(self):
        self.selected = None
        self.invalidated = 0

    def get_selected_row(self):
        return self.selected

    def select_row(self, row):
        self.selected = row

    def handler_block(self, _handler):
        pass

    def handler_unblock(self, _handler):
        pass

    def invalidate_filter(self):
        self.invalidated += 1


def window(meetings, starred, *, loaded=True, library_filter="starred"):
    stars = SimpleNamespace(loaded=loaded, starred=frozenset(starred), is_starred=lambda key: key in starred)
    rows = {m.key: SimpleNamespace(meeting=m) for m in meetings}
    win = SimpleNamespace(
        library_filter=library_filter,
        _stars=stars,
        _row_by_key=rows,
        _kept_unstarred_key=None,
        _await_star_default=False,
        selected=None,
        search_page=None,
        meeting_page=None,
        recording_page=None,
        _external_meeting=False,
        _selection_handler=None,
        _updating_rows_selection=False,
        rows=Rows(),
        get_application=lambda: None,
    )
    win.is_starred = lambda m: LibraryWindow.is_starred(win, m)

    def selected(_rows, row):
        win.selected = row.meeting if row is not None else None

    win._selected = selected
    return win


def test_starred_with_no_stars_auto_selects_nothing(tmp_path):
    newest, older = meeting(tmp_path, "Recording-b", 2000), meeting(tmp_path, "Recording-a", 1000)
    win = window([newest, older], set())
    assert LibraryWindow._default_row(win) is None
    assert not any(LibraryWindow._row_visible(win, row) for row in win._row_by_key.values())


def test_starred_auto_selects_the_first_starred_meeting_not_the_newest(tmp_path):
    newest, older = meeting(tmp_path, "Recording-b", 2000), meeting(tmp_path, "Recording-a", 1000)
    win = window([newest, older], {older.key})
    assert LibraryWindow._default_row(win).meeting is older
    # All keeps the plain first saved meeting.
    win.library_filter = "all"
    assert LibraryWindow._default_row(win).meeting is newest


def test_a_selected_unstarred_row_is_not_exempt_unless_unstarred_while_open(tmp_path):
    newest, older = meeting(tmp_path, "Recording-b", 2000), meeting(tmp_path, "Recording-a", 1000)
    win = window([newest, older], {older.key})
    win.selected = newest
    assert not LibraryWindow._row_visible(win, win._row_by_key[newest.key])
    win._kept_unstarred_key = newest.key
    assert LibraryWindow._row_visible(win, win._row_by_key[newest.key])
    win.selected = older
    assert not LibraryWindow._row_visible(win, win._row_by_key[newest.key])


def test_unstarring_the_open_meeting_keeps_it_listed_until_selection_moves(tmp_path):
    newest, older = meeting(tmp_path, "Recording-b", 2000), meeting(tmp_path, "Recording-a", 1000)
    starred = {newest.key, older.key}
    win = window([newest, older], starred)
    win._stars.toggle = lambda key: (starred.discard(key), False)[1]
    win.star_allowed = lambda m: True
    win.selected = newest
    assert LibraryWindow.toggle_star(win) is False
    assert win._kept_unstarred_key == newest.key
    assert LibraryWindow._row_visible(win, win._row_by_key[newest.key])
    # Unstarring a different, closed meeting does not keep it.
    win2 = window([newest, older], {newest.key, older.key})
    win2._stars.toggle = lambda key: False
    win2.star_allowed = lambda m: True
    win2.selected = newest
    LibraryWindow.toggle_star(win2, older)
    assert win2._kept_unstarred_key is None


def test_stars_loading_after_the_scan_select_the_first_starred_meeting(tmp_path):
    newest, older = meeting(tmp_path, "Recording-b", 2000), meeting(tmp_path, "Recording-a", 1000)
    win = window([newest, older], {older.key}, loaded=False)
    assert LibraryWindow._default_row(win) is None and win._await_star_default
    win._stars.loaded = True
    LibraryWindow._select_default_row(win)
    assert win.rows.selected.meeting is older and win.selected is older


def test_late_selection_never_overrides_what_the_user_opened(tmp_path):
    newest, older = meeting(tmp_path, "Recording-b", 2000), meeting(tmp_path, "Recording-a", 1000)
    win = window([newest, older], {older.key})
    win.search_page = object()
    LibraryWindow._select_default_row(win)
    assert win.rows.selected is None and win.selected is None


def test_moving_or_clearing_the_selection_releases_the_kept_meeting(tmp_path):
    newest, older = meeting(tmp_path, "Recording-b", 2000), meeting(tmp_path, "Recording-a", 1000)
    win = window([newest, older], {older.key})
    win.selected, win._kept_unstarred_key = newest, newest.key
    LibraryWindow._selection_moved(win, win.rows, win._row_by_key[newest.key])
    assert win._kept_unstarred_key == newest.key, "a rebuild reselecting the same meeting keeps it"
    LibraryWindow._selection_moved(win, win.rows, None)
    assert win._kept_unstarred_key is None
    assert not LibraryWindow._row_visible(win, win._row_by_key[newest.key])
