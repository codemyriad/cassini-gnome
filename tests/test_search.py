# SPDX-License-Identifier: GPL-3.0-or-later
"""Real FTS, D14 local dates/AND filters, immutable snapshot updates and cache recovery."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import test_library_vectors

from cassini_talk.library import LibraryScanner, LibrarySnapshot
from cassini_talk.portable import Word
from cassini_talk.search_index import SearchIndex, date_range, marked_markup

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
    before = two.db.total_changes
    two.sync(snapshot)
    assert two.db.total_changes == before
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
    from cassini_talk import ux_formats as fmt
    from cassini_talk.library import LibrarySpeaker

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
    from cassini_talk.search_index import excerpt_markup

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


def test_cancelled_sync_rolls_back_database_and_all_memory_then_can_retry(index, monkeypatch):
    original = tuple(index.current.values())
    old_hits = search(index, speaker="Marta")
    old_turns, old_signatures, old_current = index.turns, index.signatures, index.current
    updates = tuple(replace(m, words=(Word("new", 10, 20, "replacement"),)) for m in original)
    calls = 0
    check = index._check_cancelled

    def cancel_second_meeting():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InterruptedError("Synthetic cancellation after first meeting SQL writes")

    monkeypatch.setattr(index, "_check_cancelled", cancel_second_meeting)
    with pytest.raises(InterruptedError):
        index.sync(LibrarySnapshot(meetings=updates))
    assert index.turns is old_turns and index.signatures is old_signatures and index.current is old_current
    monkeypatch.setattr(index, "_check_cancelled", check)
    assert search(index, speaker="Marta") == old_hits
    index.sync(LibrarySnapshot(meetings=updates))
    assert len(search(index, "replacement")) == len(updates)
    assert not search(index, "budget")


def test_sql_failure_leaves_memory_and_removed_rows_consistent(index):
    import sqlite3

    original = tuple(index.current.values())
    meeting = next(m for m in original if m.title == "Budget review")
    updated = replace(meeting, words=(Word("new", 0, 1, "new phrase"),))
    before = index.turns, index.signatures, index.current
    expected = search(index, speaker="Marta")
    index.db.execute("CREATE TRIGGER fail_sync BEFORE INSERT ON meetings BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        index.sync(LibrarySnapshot(meetings=(updated,)))
    assert all(
        current is old for current, old in zip((index.turns, index.signatures, index.current), before, strict=True)
    )
    assert search(index, speaker="Marta") == expected
    index.db.execute("DROP TRIGGER fail_sync")
    index.sync(LibrarySnapshot(meetings=(updated,)))
    assert len(search(index, "phrase")) == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "journal-symlink"])
def test_search_cache_cannot_follow_links_into_library(index, tmp_path, kind):
    import os

    root, cache = tmp_path / "Library", tmp_path / "cache-linked"
    root.mkdir()
    one = SearchIndex(root, cache)
    path = one.path
    one.close()
    outside = root / "transcript.json"
    outside.write_bytes(b"Keep these Library bytes")
    before = outside.stat().st_mode
    if kind == "journal-symlink":
        path.with_name(path.name + "-journal").symlink_to(outside)
    else:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(outside)
        else:
            os.link(outside, path)
    with pytest.raises((OSError, ValueError)):
        SearchIndex(root, cache)
    assert outside.read_bytes() == b"Keep these Library bytes" and outside.stat().st_mode == before


def test_corrupt_fts_shadow_tables_rebuild(index):
    root = Path("/fts-corrupt-root")
    cache = index.path.parent / "corrupt-fts"
    one = SearchIndex(root, cache)
    snapshot = LibrarySnapshot(meetings=tuple(index.current.values()))
    one.sync(snapshot)
    one.db.execute("DELETE FROM hits_idx")
    one.db.execute("DELETE FROM hits_data")
    one.db.commit()
    # Some SQLite versions now check FTS in quick_check too; the explicit
    # FTS integrity command also covers versions that only check B-trees.
    one.close()
    two = SearchIndex(root, cache)
    two.sync(snapshot)
    assert len(search(two, speaker="Marta")) == 4
    two.close()


def test_trash_erases_removed_text_from_search_cache_pages(index):
    meeting = next(iter(index.current.values()))
    secret = "cassinitrashuniqueforensictext"
    changed = replace(meeting, words=(Word(speaker="me", start_ms=0, end_ms=500, text=secret),))
    index.sync(LibrarySnapshot(meetings=(changed,)))
    assert secret.encode() in index.path.read_bytes()
    index.sync(LibrarySnapshot())
    assert not search(index, secret)
    assert secret.encode() not in index.path.read_bytes(), "Trash left removed text in FTS/cache pages"


def test_d38_cleaned_quotes_keep_search_seek_on_the_original_item(index):
    selected = next(iter(index.current.values()))
    words = (Word("a", 0, 10, '"'), Word("a", 100, 200, "needle"), Word("b", 2000, 2100, '"other'))
    index.sync(LibrarySnapshot(meetings=(replace(selected, words=words),)))
    (hit,) = search(index, "needle")
    assert hit.position_ms == 100 and hit.marked == "\x01needle\x02"
    assert index.turns[selected.key][0].words is not None
    assert tuple(w.text for turn in index.turns[selected.key] for w in turn.words) == ('"', "needle", '"other')


def test_d38_rejoined_interrupted_turn_never_pairs_quotes_across_speakers(index, tmp_path):
    # Regression (re-review 118): build_turns rejoins A's run around B's
    # overlapping interjection, and the preview and reader used to pair A's
    # two quotes across B into “yes then no”. Every surface pairs per
    # consecutive speaker run, so both unmatched quotes drop everywhere.
    from cassini_talk import export_model
    from cassini_talk.meeting_text import item_markup, reading_turns
    from cassini_talk.transcript import build_turns, visible_turns

    words = (
        Word("a", 0, 1000, 'He said "yes'),
        Word("b", 900, 1100, "okay"),
        Word("a", 1050, 2000, 'then no"'),
    )
    reader = build_turns(list(words))
    assert len(visible_turns(reader)) == 1 and reader[1].host is reader[0], "fixture must rejoin A around B"
    expected = ["He said yes", "okay", "then no"]
    shown = {
        "reader": [reader[0].text, reader[1].text],
        "preview": [t.text for t in LibraryScanner(tmp_path)._preview("x", words)],
        "meeting": [t.text for t in reading_turns(list(words))],
    }
    selected = next(iter(index.current.values()))
    index.sync(LibrarySnapshot(meetings=(replace(selected, words=words),)))
    shown["search"] = [t.text for t in index.turns[selected.key]]
    items = [dict(speaker=w.speaker, startMs=w.start_ms, text=w.text) for w in words]
    exported = export_model.markdown(dict(title="T", speakers=[], items=items)).split("## Transcript", 1)[1]
    shown["markdown"] = [line for line in exported.splitlines() if line and not line.startswith("**")]
    assert shown == {
        "reader": ["He said yes then no", "okay"],
        "preview": ["He said yes then no"],
        "meeting": expected,
        "search": expected,
        "markdown": expected,
    }
    meeting_markup = "".join(item_markup(t.words, 0, values=t.items) for t in reading_turns(list(words)))
    assert not any("“" in v or "”" in v for v in [*map(str, shown.values()), meeting_markup])
    by_item = {id(w): v for t in reader for w, v in zip(t.words, t.items, strict=True)}
    assert [by_item[id(w)] for w in words] == expected
