# SPDX-License-Identifier: GPL-3.0-or-later
"""Native frame budgets for one four-hour viewer paragraph with 899 replies."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from exact_budgets import (
    FIRST_PAINT_CEILING_MS,
    FIRST_PAINT_TARGET_MS,
    FRAME_CPU_CEILING_MS,
    FRAME_CPU_TARGET_MS,
    check_cpu_budget,
)
from gi.repository import GLib
from transcript_long_turn import long_turn_words

from cassini_talk.meeting_text import reading_turns, speaker_turns


def long_turn_meeting(model):
    words = long_turn_words()
    return replace(model, words=words, turns=reading_turns(words), listening_turns=speaker_turns(words))


def check_long_turn(window, model, frames_type, paint, until, flicks):
    # Match production loading and keep fixture preparation out of the UI
    # thread without retaining its extra model during the earlier RSS checks.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(long_turn_meeting, model)
        future.add_done_callback(lambda _: GLib.idle_add(lambda: GLib.SOURCE_REMOVE))
        until(future.done)
        model = future.result()
    assert len(model.turns) == 1
    frames = frames_type(window)
    started = time.thread_time_ns()
    window.open_meeting(model, external=True)
    page = window.meeting_page
    paint(window)
    until(lambda: page.list.viewport_ready and bool(page.bound))
    first = (time.thread_time_ns() - started) / 1e6
    until(lambda: page.list.ready)
    paint(window)
    frames.close()
    report = dict(
        words=len(model.words),
        semanticRows=len(model.turns),
        layoutRows=len(page._turns),
        openFirstPaintCpuMs=first,
        openWorstFrameCpuMs=max(frames.cpu),
        buildSliceMaxCpuMs=page.list.build_slice_cpu_max_ns / 1e6,
    )
    check_cpu_budget(
        "longRejoinedTurn",
        "openFirstPaintCpuMs",
        first,
        target_ms=FIRST_PAINT_TARGET_MS,
        ceiling_ms=FIRST_PAINT_CEILING_MS,
    )
    check_cpu_budget(
        "longRejoinedTurn",
        "openWorstFrameCpuMs",
        report["openWorstFrameCpuMs"],
        target_ms=FRAME_CPU_TARGET_MS,
        ceiling_ms=FRAME_CPU_CEILING_MS,
    )
    check_cpu_budget(
        "longRejoinedTurn",
        "buildSliceMaxCpuMs",
        report["buildSliceMaxCpuMs"],
        target_ms=FRAME_CPU_TARGET_MS,
        ceiling_ms=FRAME_CPU_CEILING_MS,
    )
    until(lambda: window.player._prerolled)
    report["paused"] = flicks(window, page, False, label="longRejoinedTurn.paused")
    report["playingFollowOff"] = flicks(window, page, True, label="longRejoinedTurn.playingFollowOff")
    frames = frames_type(window)
    original = window.get_default_size()
    width = page.list.layout_width
    window.set_default_size(860, original[1])
    until(lambda: page.list.layout_width < width and page.list.ready)
    paint(window)
    window.set_default_size(*original)
    until(lambda: page.list.layout_width == width and page.list.ready)
    paint(window)
    frames.close()
    report["resizeWorstFrameCpuMs"] = max(frames.cpu)
    check_cpu_budget(
        "longRejoinedTurn",
        "resizeWorstFrameCpuMs",
        report["resizeWorstFrameCpuMs"],
        target_ms=FRAME_CPU_TARGET_MS,
        ceiling_ms=FRAME_CPU_CEILING_MS,
    )
    # Seek/follow deep inside the semantic paragraph and activate a real chip.
    target = next(row for row in reversed(page._turns[:-10]) if row.asides)
    word = target.asides[0][1].words[0]
    page._follow_until = 0
    page.player.seek(word.start_ms)
    until(lambda: page._reading_index == page._follow_rows.at(word.start_ms) and target.index in page.bound)
    paint(window)
    label = page.bound[target.index][1]
    assert label.emit("activate-link", "aside:0:0")
    until(lambda: page.player.position == word.start_ms)
    assert 'background="#fbe7a1"' in label.get_label()
    report["deepChipSeek"] = True
    window.back_to_library()
    paint(window)
    return report
