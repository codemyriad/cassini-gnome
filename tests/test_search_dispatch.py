# SPDX-License-Identifier: GPL-3.0-or-later
"""A stale worker completion must consume its already scheduled debounce."""

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
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


def test_title_result_has_no_speaker_even_without_any_transcript_rows():
    from cassini_talk.search_index import SearchHit

    page = SimpleNamespace(_meetings={}, _turns={})
    hit = SearchHit("audio-only", -1, "", 0, "", "", "", title_markup="Interview")
    assert search_page.SearchPage._speaker(page, hit) is None


def test_playability_change_refreshes_the_snapshot_when_other_search_fields_are_identical():
    from dataclasses import replace
    from pathlib import Path

    from cassini_talk.library import CaptureTrack, LibrarySnapshot, Meeting

    track = CaptureTrack(Path("/fixture/track.wav"), 48000, 48000, 0, True)
    meeting = Meeting(
        id="capture",
        directory=Path("/fixture/Recording-capture"),
        title="Synthetic English meeting",
        started_at_ms=1000,
        duration_ms=1000,
        has_audio=True,
        tracks=(track,),
        lifecycle="recording",
    )
    requested = []
    app = SimpleNamespace(voice_store=SimpleNamespace(match_signature=lambda: ()))
    page = SimpleNamespace(
        window=SimpleNamespace(get_application=lambda: app),
        _snapshot_signature=None,
        request=lambda: requested.append("refresh"),
    )

    def update(value):
        search_page.SearchPage.update(page, LibrarySnapshot(meetings=(value,)))

    update(meeting)
    update(replace(meeting, lifecycle="saving"))
    assert requested == ["refresh"]
    finished = replace(meeting, lifecycle="saved")
    update(finished)
    assert requested == ["refresh", "refresh"]
    update(finished)
    assert requested == ["refresh", "refresh"]
    update(replace(finished, tracks=(replace(track, start_offset_ms=None),)))
    assert requested == ["refresh", "refresh", "refresh"]
