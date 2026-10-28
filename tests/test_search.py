# SPDX-License-Identifier: GPL-3.0-or-later
"""Real FTS, D14 local dates/AND filters, immutable snapshot updates and cache recovery."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import test_library_vectors

from cassini_viewer.library import LibraryScanner, LibrarySnapshot
from cassini_viewer.portable import Word
from cassini_viewer.search_index import SearchIndex, date_range, marked_markup

NOW = 1793107080000
shared_library = test_library_vectors.shared_library


@pytest.fixture
def index(shared_library, tmp_path):
    scanner = LibraryScanner(shared_library / "main/root")
    value = SearchIndex(scanner.root, tmp_path / "cache")
    value.sync(scanner.scan())
    yield value
    value.close()


def search(index, query="budget", **kwargs):
    return index.query(query, now_ms=NOW, tz="Europe/Rome", **kwargs)


def test_fixture_four_places_three_meetings_in_transcript_order(index):
    hits = search(index, speaker="Marta")
    assert len(hits) == 4
    assert len({hit.key for hit in hits}) == 3
    assert [index.current[h.key].title for h in hits] == [
        "Budget review",
        "Budget review",
        "Weekly sync, product team",
        "Planning, Q4",
    ]
    assert [h.position_ms for h in hits] == [4000, 872000, 1870000, 527000]
    assert "\x01budget\x02" in hits[0].marked
    assert index.speakers() == tuple(sorted(index.speakers(), key=str.casefold))
    assert "Marta" in index.speakers()
    assert not any(name.startswith("Speaker ") for name in index.speakers())
    assert index.path.parent.name == "cache"
    assert index.path.parent not in next(iter(index.current.values())).directory.parents


def test_filters_and_literal_phrase(index):
    assert len(search(index, speaker="Marta", date="today", own=True)) == 2
    assert len(search(index, speaker="Marta", date="week")) == 2
    assert len(search(index, speaker="Marta", date="month")) == 4
    assert not search(index, speaker="unknown")
    assert not search(index, "budget OR impossible")
    assert not search(index, '" OR *')
    assert not search(index, "")
    assert not search(index, "***")
    assert len(search(index, "the budget", speaker="Marta")) >= 2
    assert search(index, "BUDGET", speaker="marta") == search(index, speaker="Marta")


def test_index_updates_removal_timing_and_unchanged_no_rewrite(index):
    meetings = tuple(index.current.values())
    index.sync(LibrarySnapshot(meetings=meetings))
    writes = index.db.total_changes
    index.sync(LibrarySnapshot(meetings=tuple(replace(m, title=m.title + " renamed") for m in meetings)))
    assert index.db.total_changes == writes
    selected = next(m for m in meetings if m.title == "Budget review")
    first = selected.words[0]
    changed = replace(
        selected,
        words=(
            Word(speaker="new", start_ms=12, end_ms=40, text="phrase across"),
            Word(speaker="new", start_ms=40, end_ms=80, text="items & <unsafe>"),
        ),
    )
    index.sync(LibrarySnapshot(meetings=(changed,)))
    (hit,) = search(index, "across items")
    assert hit.position_ms == 12
    assert "&amp;" in marked_markup(hit.marked) and "&lt;unsafe&gt;" in marked_markup(hit.marked)
    assert not search(index, first.text + " impossible")
    index.sync(LibrarySnapshot(unavailable=True))
    assert not search(index)
    assert not index.turns


def test_reopen_reuses_persisted_index(index):
    root = Path("/some/root")
    cache = index.path.parent / "second"
    one = SearchIndex(root, cache)
    snapshot = LibrarySnapshot(meetings=tuple(index.current.values()))
    one.sync(snapshot)
    expected = search(one, speaker="Marta")
    one.close()
    two = SearchIndex(root, cache)
    two.sync(snapshot)
    assert two.db.total_changes == 0
    assert search(two, speaker="Marta") == expected
    two.close()


def test_dates_use_local_midnight_and_calendar_days_across_dst():
    zone = ZoneInfo("Europe/Rome")
    now = int(datetime(2026, 10, 27, 12, tzinfo=zone).timestamp() * 1000)
    lower, upper = date_range("week", now, zone)
    assert datetime.fromtimestamp(lower / 1000, zone).isoformat() == "2026-10-21T00:00:00+02:00"
    assert datetime.fromtimestamp(upper / 1000, zone).isoformat() == "2026-10-28T00:00:00+01:00"
    assert upper - lower == (7 * 24 + 1) * 3600000


def test_corrupt_cache_is_rebuilt_and_private(index):
    root = Path("/other/root")
    cache = index.path.parent / "corrupt"
    one = SearchIndex(root, cache)
    path = one.path
    one.close()
    path.write_bytes(b"broken SQLite cache")
    two = SearchIndex(root, cache)
    two.sync(LibrarySnapshot(meetings=tuple(index.current.values())))
    assert len(search(two, speaker="Marta")) == 4
    assert path.stat().st_mode & 0o777 == 0o600
    two.close()
    with pytest.raises(ValueError, match="outside"):
        SearchIndex(root, root / "cache")


def test_unknown_date_sorts_last_and_filters_exclude_it(index):
    meeting = next(m for m in index.current.values() if m.title == "Budget review")
    unknown = replace(
        meeting, directory=meeting.directory.parent / "Received-unknown", started_at_ms=None, origin="received"
    )
    index.sync(LibrarySnapshot(meetings=(unknown, meeting)))
    hits = search(index, speaker="Marta")
    assert [h.key for h in hits] == [meeting.key, meeting.key, unknown.key, unknown.key]
    assert len(search(index, speaker="Marta", own=True)) == 2
    assert len(search(index, speaker="Marta", date="month")) == 2


def test_close_interrupts_large_fts_query(index):
    from threading import Event

    cancelled = Event()
    index.cancelled = cancelled
    cancelled.set()
    with pytest.raises(InterruptedError):
        search(index)


def test_received_named_speaker_without_sidecar_provenance_is_in_menu(index):
    from cassini_viewer import ux_formats as fmt
    from cassini_viewer.library import LibrarySpeaker

    meeting = next(iter(index.current.values()))
    meeting = replace(
        meeting,
        speakers=(
            LibrarySpeaker("a", "Marta", None, 0, 0),
            LibrarySpeaker("b", "marta", "user", 0, 0),
            LibrarySpeaker("c", "Speaker 7", None, 0, 0),
        ),
    )
    index.sync(LibrarySnapshot(meetings=(meeting,)))
    assert index.speakers() == ("Marta",)
    assert fmt.search_date(None, now_ms=NOW, tz="Europe/Rome") == "Date unknown"


def test_long_turn_excerpt_keeps_hit_and_markup_bounded():
    from cassini_viewer.search_index import excerpt_markup

    marked = "<unsafe> " * 10000 + "\x01budget & costs\x02" + " tail" * 10000
    excerpt = excerpt_markup(marked)
    assert "budget &amp; costs" in excerpt
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert len(excerpt) < 1500
    assert excerpt.count("<span ") == excerpt.count("</span>") == 1
    assert "<unsafe>" not in excerpt


def test_received_items_are_ordered_by_start_with_file_order_ties(index):
    meeting = next(m for m in index.current.values() if m.title == "Budget review")
    reordered = replace(meeting, words=tuple(reversed(meeting.words)))
    index.sync(LibrarySnapshot(meetings=(reordered,)))
    assert [h.position_ms for h in search(index, speaker="Marta")] == [4000, 872000]
    equal = replace(meeting, words=(Word("second", 100, 200, "budget two"), Word("first", 100, 200, "budget one")))
    index.sync(LibrarySnapshot(meetings=(equal,)))
    assert [index.turns[h.key][h.turn].speaker for h in search(index)] == ["second", "first"]
