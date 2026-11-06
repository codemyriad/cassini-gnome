# SPDX-License-Identifier: GPL-3.0-or-later
"""A stale worker completion must consume its already scheduled debounce."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
from cassini_talk import search_page  # noqa: E402


def test_stale_completion_cancels_debounce_before_dispatching_latest_query(monkeypatch):
    events = []
    future = object()
    page = SimpleNamespace(_future=future, closed=False, _generation=2, _timer=37)
    page._dispatch = lambda: events.append(("dispatch", page._timer))
    monkeypatch.setattr(search_page.GLib, "source_remove", lambda source: events.append(("cancel", source)))
    search_page.SearchPage._complete(page, future, 1)
    assert events == [("cancel", 37), ("dispatch", 0)]
