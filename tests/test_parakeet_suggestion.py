# SPDX-License-Identifier: GPL-3.0-or-later
"""D78 capacity and state decisions stay widget/engine-free."""

import pytest

from cassini_talk import parakeet_suggestion as suggestion


@pytest.mark.parametrize(
    ("ram", "supported", "expected"),
    [(7_999_999_999, True, False), (8_000_000_000, True, True), (16_000_000_000, False, False)],
)
def test_capacity_boundary(monkeypatch, ram, supported, expected):
    monkeypatch.setattr(suggestion, "memory_bytes", lambda: ram)
    monkeypatch.setattr(suggestion, "available", lambda: supported)
    assert suggestion.capable() is expected


@pytest.mark.parametrize(
    "negative", [dict(downloaded=True), dict(busy=True), dict(downloading=True), dict(suggestible=False)]
)
def test_no_suggestion_until_eligible_and_no_repeat_for_installed_model(negative):
    state = dict(suggestible=True, downloaded=False, busy=False, downloading=False)
    assert suggestion.offer(state, False)
    assert not suggestion.offer(dict(state, **negative), False)
    assert not suggestion.offer(state, True)


@pytest.mark.parametrize(
    "limit, expected", [("max", 16_000_000_000), ("4000000000", 4_000_000_000), ("32000000000", 16_000_000_000)]
)
def test_memory_uses_physical_ram_and_honours_container_limit(monkeypatch, limit, expected):
    monkeypatch.setattr(suggestion.os, "sysconf", lambda key: 4_000_000 if key == "SC_PHYS_PAGES" else 4000)
    monkeypatch.setattr(suggestion.Path, "read_text", lambda _self: limit)
    assert suggestion.memory_bytes() == expected
