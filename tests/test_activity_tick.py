# SPDX-License-Identifier: GPL-3.0-or-later
"""Activity tick skips redundant library/meeting redraws when nothing changed."""

from datetime import UTC
from types import SimpleNamespace

import pytest

pytest.importorskip("gi", reason="LibraryWindow requires PyGObject")

from cassini_talk import ux_formats as fmt
from cassini_talk.library_window import LibraryWindow


def _window(*, now_ms=1_700_000_000_000, tz=UTC, activity=None, recording_page=None, pending_recording=None):
    updated = []
    refreshed = []
    connected = []
    app = SimpleNamespace(
        build_refresh=SimpleNamespace(capture_changed=lambda: None),
        get_windows=lambda: [],
        _pending_recording=pending_recording,
        recorder=None,
        _browser_stops=(),
        library_recovery=None,
    )
    snapshot = SimpleNamespace(meetings=())
    source = SimpleNamespace(snapshot=activity or {}, connected=True)
    window = SimpleNamespace(
        get_application=lambda: app,
        _activity_source=source,
        _fixture_activity=None,
        _activity=dict(activity or {}),
        monitor=SimpleNamespace(refresh_now=lambda: refreshed.append(True)),
        _update_live=lambda: None,
        meeting_page=SimpleNamespace(set_connected=lambda value: connected.append(value)),
        recording_page=recording_page,
        snapshot=snapshot,
        _source_snapshot=snapshot,
        now_ms=now_ms,
        tz=tz,
        _activity_day=(fmt._local(now_ms, tz).date(), tz),
        _updated=lambda snap: updated.append(snap),
    )
    return window, updated, refreshed, connected


def test_idle_activity_tick_skips_redundant_updated():
    window, updated, refreshed, connected = _window()
    LibraryWindow._activity_tick(window)
    assert updated == []
    assert refreshed == []
    assert connected == [True]


def test_activity_tick_reprojects_on_activity_change_or_local_midnight():
    window, updated, refreshed, _connected = _window()
    # 1. Activity changes -> refresh + _updated.
    window._activity_source.snapshot = {"m-1": "recording"}
    LibraryWindow._activity_tick(window)
    assert len(updated) == 1
    assert len(refreshed) == 1

    # 2. Next tick with same activity and same day -> no redundant _updated.
    LibraryWindow._activity_tick(window)
    assert len(updated) == 1
    assert len(refreshed) == 1

    # 3. Local midnight crossing -> _updated runs to re-group dates.
    window.now_ms += 24 * 60 * 60 * 1000
    LibraryWindow._activity_tick(window)
    assert len(updated) == 2
    assert len(refreshed) == 1


def test_show_right_meeting_skips_duplicate_select_during_row_sync():
    selected = []
    rebound = []
    visible = []
    slot = object()
    player = object()
    meeting = object()
    page = SimpleNamespace(
        get_parent=lambda: slot,
        player=player,
        rebind_player=lambda p: rebound.append(p),
        select_meeting=lambda m, *, reset_scroll=False: selected.append((m, reset_scroll)),
    )
    window = SimpleNamespace(
        meeting_page=page,
        meeting_slot=slot,
        recording_page=None,
        _external_meeting=False,
        player=player,
        _updating_rows_selection=True,
        _updated_meeting=meeting,
        detail_stack=SimpleNamespace(set_visible_child_name=lambda name: visible.append(name)),
    )

    # Already updated in _updated() with the same meeting & player -> skip duplicate select_meeting.
    LibraryWindow._show_right_meeting(window, meeting, rebind_player=True)
    assert selected == []
    assert rebound == []
    assert visible == ["meeting"]

    # If the player changed, rebind and refresh the page.
    new_player = object()
    window.player = new_player
    LibraryWindow._show_right_meeting(window, meeting, rebind_player=True)
    assert rebound == [new_player]
    assert selected == [(meeting, False)]

    # Outside _updated() row sync, select_meeting always runs.
    window._updating_rows_selection = False
    window.player = player
    LibraryWindow._show_right_meeting(window, meeting)
    assert selected == [(meeting, False), (meeting, False)]
