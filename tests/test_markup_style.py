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


def test_style_notifications_defer_and_coalesce_before_reading_the_new_css(monkeypatch):
    page_class = page_type()
    from gi.repository import GLib

    callbacks = []
    monkeypatch.setattr(GLib, "idle_add", lambda callback: callbacks.append(callback) or 12)
    page = SimpleNamespace(
        _closed=False,
        _style_source=0,
        meeting=object(),
        update=Mock(),
        bound={0: ("turn", "words", "row")},
        _render_words=Mock(),
    )
    page._style_ready = lambda: page_class._style_ready(page)
    page_class._style_changed(page)
    page_class._style_changed(page)
    assert len(callbacks) == 1 and page._style_source == 12
    page.update.assert_not_called()
    assert callbacks[0]() == GLib.SOURCE_REMOVE
    page.update.assert_not_called()
    page._render_words.assert_called_once_with("turn", "words")
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
