# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone worker projection, offscreen long paragraph and deferred search."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import thread_time_ns

from gi.repository import GLib
from transcript_long_turn import long_turn_words

from cassini_talk.meeting_text import reader_turns
from cassini_talk.portable import Word


def check_search_supersession(window, file, paint, until):
    view = window._exact_transcript
    for dismiss in (False, True):
        window.open_file(file)
        until(lambda: bool(view.rows))
        window.search_bar.set_search_mode(True)
        window.search_entry.set_text("instrumentation")
        until(lambda: bool(window.matches))
        window.match_cursor = len(window.matches) - 2
        window._step_match(1)
        assert window._word_scroll is not None and view._pending_scroll is not None and not view.ready
        if dismiss:
            window.search_bar.set_search_mode(False)
        else:
            window.search_entry.set_text("The")
            until(
                lambda: window.matches and window.turns[window.matches[0][0]].words[window.matches[0][1]].text == "The"
            )
            until(lambda: window._word_scroll is None)
        assert view._pending_scroll is None and window._word_scroll is None
        paint(window)
        retained = view._value()
        until(lambda: view.ready)
        paint(window)
        assert abs(view._value() - retained) <= 1, (dismiss, retained, view._value())
    window.search_bar.set_search_mode(False)


def check_long_reader(window, paint, until):
    def project():
        words = tuple(Word("person-2", i * 61000, i * 61000 + 500, "Introduction.") for i in range(20)) + tuple(
            replace(word, start_ms=word.start_ms + 1220000, end_ms=word.end_ms + 1220000) for word in long_turn_words()
        )
        oversized_chip = reader_turns(
            [Word("person-0", 0, 1000, "Opening.")]
            + [Word("person-1", 900, 1100, "Reply") for _ in range(400)]
            + [Word("person-0", 1000, 2000, "Closing.")]
        )
        return reader_turns(words), oversized_chip

    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(project)
        future.add_done_callback(lambda _: GLib.idle_add(lambda: GLib.SOURCE_REMOVE))
        until(future.done)
        turns, oversized_chip = future.result()
    window.player.pause()
    window.settings.set_boolean("follow-along", False)
    view = window._exact_transcript
    frame_start = [0]
    cpu = []
    clock = window.get_frame_clock()
    before = clock.connect("before-paint", lambda *_: frame_start.__setitem__(0, thread_time_ns()))
    after = clock.connect("after-paint", lambda *_: cpu.append((thread_time_ns() - frame_start[0]) / 1e6))
    start = thread_time_ns()
    try:
        window._set_turns(turns)
        paint(window)
        until(lambda: view.viewport_ready)
        first = (thread_time_ns() - start) / 1e6
        host = next(turn for turn in turns if len(turn.words) > 70000)
        assert turns.visible_index[host.index] not in view.visible_rows
        assert 0 in view.rows and view._value() == 0 and not view.ready
        until(lambda: view.ready)
        paint(window)
        assert view._value() == 0, "Offscreen paragraph indexing moved the short introduction"
    finally:
        clock.disconnect(before)
        clock.disconnect(after)
    report = dict(
        layoutRows=len(view.items),
        firstPaintCpuMs=first,
        worstFrameCpuMs=max(cpu),
        buildSliceMaxCpuMs=view.build_slice_cpu_max_ns / 1e6,
    )
    assert first < 100 and max(cpu) < 32 and report["buildSliceMaxCpuMs"] < 32, report
    # Both a late host word and a late folded chip still use original URIs.
    chip = host.asides[len(host.asides) * 3 // 4][1]
    for key in ((host.index, len(host.words) * 3 // 4), (chip.index, 0)):
        window._scroll_to_word(*key, center=True)
        until(lambda: window._word_scroll is None)
        paint(window)
        row = window._realized_turn(*key)
        assert row is not None
        uri = f"w:{key[0]}/{key[1]}"
        assert uri in row.link_offsets
        ok, bounds = row.compute_bounds(window.transcript_scroller)
        assert (
            ok and bounds.get_y() < window.transcript_scroller.get_height() and bounds.get_y() + bounds.get_height() > 0
        )
        word = turns[key[0]].words[key[1]]
        row.words.emit("activate-link", uri)
        until(lambda word=word: window.player.position >= word.start_ms and window.player.playing)
        window.player.pause()
    window._set_turns(oversized_chip)
    until(lambda: view.ready)
    key = (oversized_chip[0].asides[0][1].index, 250)
    position = oversized_chip.word_rows[key]
    assert not view.items[position].words, "Fixture must exercise a chip-only physical row"
    window._scroll_to_word(*key, center=True)
    until(lambda: window._word_scroll is None)
    paint(window)
    row = window._realized_turn(*key)
    assert row is not None and f"w:{key[0]}/{key[1]}" in row.link_offsets
    report["chipOnlyContinuationSeek"] = True
    print("LONG_STANDALONE_READER", json.dumps(report), flush=True)
