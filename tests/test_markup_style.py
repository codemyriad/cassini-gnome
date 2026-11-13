# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Theme events coalesce after CSS updates and cannot outlive the page."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def page_type():
    gi = pytest.importorskip("gi")
    for name, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
        gi.require_version(name, version)
    from cassini_talk.meeting_page import MeetingPage

    return MeetingPage


@pytest.mark.parametrize("exact", [False, True])
def test_style_notifications_defer_and_coalesce_before_reading_the_new_css(monkeypatch, exact):
    page_class = page_type()
    from gi.repository import GLib

    callbacks = []
    monkeypatch.setattr(GLib, "idle_add", lambda callback: callbacks.append(callback) or 12)
    page = SimpleNamespace(
        _closed=False,
        _exact=False,
        _style_source=0,
        meeting=object(),
        update=Mock(),
        bound={0: ("turn", "words", "row")},
        _render_words=Mock(),
    )
    page._exact, page.list = exact, Mock()
    page._style_ready = lambda: page_class._style_ready(page)
    page_class._style_changed(page)
    page_class._style_changed(page)
    assert len(callbacks) == 1 and page._style_source == 12
    page.update.assert_not_called()
    assert callbacks[0]() == GLib.SOURCE_REMOVE
    page.update.assert_not_called()
    page._render_words.assert_called_once_with("turn", "words")
    assert page.list.invalidate.call_count == int(exact)
    assert page._style_source == 0


def test_close_cancels_pending_style_refresh_and_disconnects_both_notifications(monkeypatch):
    page_class = page_type()
    from gi.repository import GLib

    removed = []
    monkeypatch.setattr(GLib, "source_remove", removed.append)
    page = SimpleNamespace(_closed=True, _style_source=12, _style_signals=[5, 6], _style_manager=Mock(), update=Mock())
    page_class._clear_style_refresh(page)
    assert removed == [12] and page._style_source == 0 and page._style_signals == []
    assert [call.args[0] for call in page._style_manager.disconnect.call_args_list] == [5, 6]
    page_class._style_ready(page)
    page_class._style_changed(page)
    page.update.assert_not_called()


def test_dark_headers_update_synchronously_but_word_colours_wait_for_css(monkeypatch):
    page_class = page_type()
    from gi.repository import GLib

    callbacks = []
    monkeypatch.setattr(GLib, "idle_add", lambda callback: callbacks.append(callback) or 12)
    page = SimpleNamespace(
        _closed=False,
        _exact=False,
        _style_source=0,
        meeting=object(),
        update=Mock(),
        bound={0: ("turn", "words", "row")},
        _render_words=Mock(),
    )
    page._style_ready = lambda: page_class._style_ready(page)
    page_class._style_changed(page, None, SimpleNamespace(name="dark"))
    page.update.assert_called_once_with(page.meeting)
    page._render_words.assert_not_called()
    callbacks[0]()
    page.update.assert_called_once_with(page.meeting)
    page._render_words.assert_called_once_with("turn", "words")


def test_switching_meetings_keeps_inflight_merge_bound_to_origin_directory(monkeypatch, tmp_path):
    from concurrent.futures import Future
    from pathlib import Path

    page_class = page_type()
    from gi.repository import GLib

    from cassini_talk import speaker_edits

    idle_Queue = []
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *args: idle_Queue.append((cb, args)) or 1)

    dir_a = tmp_path / "meeting-a"
    dir_b = tmp_path / "meeting-b"
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            fut = Future()
            submitted.append((fut, fn, args, kwargs))
            return fut

    app = SimpleNamespace(recompute_voices=Mock())
    window = SimpleNamespace(
        listen=SimpleNamespace(active=True, speaker="s2", turns_of=lambda _s: [1]),
        monitor=SimpleNamespace(_closed=False, _executor=FakeExecutor(), scanner=object(), refresh_now=Mock()),
        get_application=lambda: app,
        toast=Mock(),
        toasts=SimpleNamespace(add_toast=Mock()),
    )
    meeting_a = SimpleNamespace(
        key="key-a",
        directory=Path(dir_a),
        metadata_editable=True,
        speakers=[
            SimpleNamespace(id="s1", name="Marta", provenance="user"),
            SimpleNamespace(id="s2", name="Speaker 2", provenance="unnamed"),
        ],
    )
    meeting_b = SimpleNamespace(
        key="key-b",
        directory=Path(dir_b),
        metadata_editable=True,
        speakers=[SimpleNamespace(id="b1", name="Luca", provenance="user")],
    )
    page = SimpleNamespace(
        _closed=False,
        meeting=meeting_a,
        window=window,
        player=SimpleNamespace(pause=Mock()),
        listening_bar=SimpleNamespace(update=Mock()),
        _speaker_future=None,
        _merge_transition=None,
    )
    page.prepare_speaker_merge = lambda s, t: page_class.prepare_speaker_merge(page, s, t)
    page._speaker_merged = lambda *a: page_class._speaker_merged(page, *a)
    page.speaker_merged = Mock()
    monkeypatch.setattr(speaker_edits, "merge_into", Mock())

    page_class.merge_speaker(page, "s2", "s1")
    assert len(submitted) == 1 and submitted[0][2][:3] == (dir_a, "s2", "s1")
    # User switches the reused right-pane page from A to B before A's merge finishes.
    page.meeting = meeting_b
    page._speaker_future = None
    fut = submitted[0][0]
    fut.set_result(None)
    assert len(idle_Queue) == 1
    cb, args = idle_Queue.pop(0)
    cb(*args)
    app.recompute_voices.assert_called_once_with(dir_a, ("s2", "s1"))
    page.speaker_merged.assert_not_called()
    window.monitor.refresh_now.assert_called_once_with()


def test_find_after_equal_content_refresh_targets_displayed_layout_chunk():
    from cassini_talk.meeting_text import reading_turns
    from cassini_talk.transcript import Word

    page_class = page_type()

    def build_turns_copy():
        seq = [Word("a", i * 100, i * 100 + 100, f"tok{i}") for i in range(200)]
        seq.insert(100, Word("b", 9950, 10250, "Right."))
        return reading_turns(seq)

    initial_turns = build_turns_copy()
    reparsed_turns = build_turns_copy()
    assert initial_turns == reparsed_turns
    assert initial_turns[0].words[180] is not reparsed_turns[0].words[180]

    # Simulate MeetingPage after an equal-content refresh (e.g. title or merged-speaker
    # rescan) where page.meeting holds newly reparsed turns while page._turns retains
    # the displayed layout chunks.
    scrolled = []
    page = SimpleNamespace(
        meeting=SimpleNamespace(turns=reparsed_turns),
        _turns=initial_turns.layout,
        _find_query="tok180",
        _find_matches=(),
        _find_index=-1,
        _find_row_highlights={},
        _sync_find_chrome=Mock(),
        _refresh_find_highlights=Mock(),
        _scroll_to_find_match=lambda: scrolled.append(page._find_matches[page._find_index].row_index),
    )
    page_class._recompute_find_matches(page, scroll=True)
    assert len(page._find_matches) == 1
    assert page._find_matches[0].row_index == 1
    assert page._find_matches[0].spans == (((1, -1), 53, 0, 6),)
    assert set(page._find_row_highlights) == {1}
    assert scrolled == [1]
