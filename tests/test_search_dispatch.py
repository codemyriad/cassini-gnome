# SPDX-License-Identifier: GPL-3.0-or-later
"""A stale worker completion must consume its already scheduled debounce."""

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from cassini_talk import search_page  # noqa: E402


def test_stale_completion_cancels_debounce_before_dispatching_latest_query(monkeypatch):
    events = []
    future = object()
    page = SimpleNamespace(_future=future, closed=False, _generation=2, _timer=37)
    page._dispatch = lambda: events.append(("dispatch", page._timer))
    monkeypatch.setattr(search_page.GLib, "source_remove", lambda source: events.append(("cancel", source)))
    search_page.SearchPage._complete(page, future, 1)
    assert events == [("cancel", 37), ("dispatch", 0)]


def test_result_speaker_uses_the_matched_chip_word_and_keeps_old_hits_compatible():
    from cassini_talk.search_index import SearchHit

    page = SimpleNamespace(
        _meetings={"m": SimpleNamespace(speaker_for=lambda speaker: speaker)},
        _turns={"m": [SimpleNamespace(speaker="host")]},
    )
    args = ("m", 0, "reply", 900, "", "", "")
    assert search_page.SearchPage._speaker(page, SearchHit(*args, speaker="chip")) == "chip"
    assert search_page.SearchPage._speaker(page, SearchHit(*args)) == "host"
