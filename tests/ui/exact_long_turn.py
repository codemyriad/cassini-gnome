# SPDX-License-Identifier: GPL-3.0-or-later
"""Native frame budgets for one four-hour viewer paragraph with 899 replies."""

import time
from dataclasses import replace

from transcript_long_turn import long_turn_words

from cassini_talk.meeting_text import reading_turns, speaker_turns


def check_long_turn(window, model, frames_type, paint, until, flicks):
    words = long_turn_words()
    turns = reading_turns(words)
    assert len(turns) == 1
    model = replace(model, words=words, turns=turns, listening_turns=speaker_turns(words))
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
        words=len(words),
        semanticRows=len(turns),
        layoutRows=len(page._turns),
        openFirstPaintCpuMs=first,
        openWorstFrameCpuMs=max(frames.cpu),
        buildSliceMaxCpuMs=page.list.build_slice_cpu_max_ns / 1e6,
    )
    assert first < 100, report
    assert report["openWorstFrameCpuMs"] < 32, report
    assert report["buildSliceMaxCpuMs"] < 32, report
    until(lambda: window.player._prerolled)
    report["paused"] = flicks(window, page, False)
    report["playingFollowOff"] = flicks(window, page, True)
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
    assert report["resizeWorstFrameCpuMs"] < 32, report
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
