# transcript.py
#
# Copyright 2026 Silvio Tomatis
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turns, interjections and search over a cassini.words.v1 transcript.

No GTK here: the window turns these into widgets.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .portable import Word

# How long a turn stays highlighted after its last word, so short gaps
# between words do not make the highlight flicker.
LINGER_MS = 300

# Ported from gocassini/cassini-viewer/src/core/overlap.ts. Judge whole
# speaker runs, not individual fragments, so competing speech stays separate.
SEAM_GAP_MS = 400
ASIDE_MAX_MS = 2000
SIDE_GAP_MS = 500
TOUCH_TOLERANCE_MS = 20


@dataclass(slots=True)
class Turn:
    """A speaker run, with words retained in their original file order."""

    index: int
    speaker: str
    words: list[Word]
    first_word: int  # index of words[0] in the whole transcript
    start_ms: int
    end_ms: int
    asides: list[tuple[int, Turn]] = field(default_factory=list)  # (after word i, turn)
    fragments: list[tuple[int, int]] = field(default_factory=list)  # word slices
    over: list[str] = field(default_factory=list)
    host: Turn | None = None  # set when this turn is shown inside another

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def word_at(self, ms: int) -> int:
        """Index of the last word that has started by *ms*, or -1."""
        starts = [w.start_ms for w in self.words]
        return bisect.bisect_right(starts, ms) - 1

    def is_active(self, ms: int) -> bool:
        return self.start_ms <= ms < self.end_ms + LINGER_MS


def _spans(words):
    """Union of audible word intervals, excluding silence and zero durations."""
    spans = []
    for word in sorted(words, key=lambda w: w.start_ms):
        if word.end_ms <= word.start_ms:
            continue
        if spans and word.start_ms <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], word.end_ms))
        else:
            spans.append((word.start_ms, word.end_ms))
    return spans


def _touches(left, right):
    return any(max(a, c) - min(b, d) <= TOUCH_TOLERANCE_MS for a, b in left for c, d in right)


def build_turns(words: list[Word]) -> list[Turn]:
    """Rejoin interrupted speaker runs without reordering their words.

    The portable format supplies words rather than Nextcloud's display blocks;
    consecutive same-speaker items are the fragment boundaries available here.
    """
    fragments = []
    for i, word in enumerate(words):
        if fragments and fragments[-1].speaker == word.speaker:
            fragment = fragments[-1]
            fragment.words.append(word)
            fragment.end_ms = max(fragment.end_ms, word.end_ms)
        else:
            fragments.append(Turn(len(fragments), word.speaker, [word], i, word.start_ms, word.end_ms))
    spans = [_spans(fragment.words) for fragment in fragments]
    turns = []
    opened = {}
    for position, fragment in enumerate(fragments):
        previous = opened.get(fragment.speaker)
        join = False
        if previous and spans[position]:
            run, last = previous
            previous_spans = spans[last]
            intervening = [span for pool in spans[last + 1 : position] for span in pool]
            join = (
                bool(previous_spans)
                and abs(spans[position][0][0] - run.end_ms) <= SEAM_GAP_MS
                and (not intervening or _touches(intervening, previous_spans + spans[position]))
            )
        if join:
            start = len(run.words)
            run.words.extend(fragment.words)
            run.fragments.append((start, len(run.words)))
            run.end_ms = max(run.end_ms, fragment.end_ms)
        else:
            run = fragment
            run.index = len(turns)
            run.fragments = [(0, len(run.words))]
            turns.append(run)
        opened[fragment.speaker] = (run, position)
    _attach_asides(turns)
    _mark_overlap(turns)
    return turns


def _attach_asides(turns: list[Turn]) -> None:
    candidates = {}
    for turn in turns:
        spans = _spans(turn.words)
        if not spans or sum(end - start for start, end in spans) > ASIDE_MAX_MS:
            continue
        best = None
        for host in turns:
            if host.speaker == turn.speaker or turn.end_ms > host.end_ms:
                continue
            fragments = [(start, end, _spans(host.words[start:end])) for start, end in host.fragments]
            before = -1
            for i, (_, _, pool) in enumerate(fragments):
                if pool and pool[0][0] < turn.start_ms:
                    before = i
            if before < 0 or before + 1 >= len(fragments):
                continue
            _, end, left = fragments[before]
            right = fragments[before + 1][2]
            if not left or not right:
                continue
            if sum(bool(pool) and turn.start_ms < pool[0][0] <= turn.end_ms for _, _, pool in fragments) > 1:
                continue
            if turn.start_ms - left[-1][1] > SIDE_GAP_MS or right[0][0] - turn.end_ms > SIDE_GAP_MS:
                continue
            score = (left[-1][1], host.end_ms - host.start_ms)
            if best is None or score > best[0]:
                best = (score, host, end - 1)
        if best:
            candidates[turn.index] = best
    for index, (_, host, after) in candidates.items():
        # No recursive chips: never hide an aside inside another aside.
        if host.index not in candidates:
            turn = turns[index]
            turn.host = host
            host.asides.append((after, turn))


def _mark_overlap(turns):
    # Like Nextcloud, report actual word overlap, not paragraph containment.
    pools = [_spans(turn.words) for turn in turns]
    for i, left in enumerate(turns):
        for j in range(i + 1, len(turns)):
            right = turns[j]
            if left.speaker == right.speaker or max(left.start_ms, right.start_ms) >= min(left.end_ms, right.end_ms):
                continue
            a = b = overlap = 0
            while a < len(pools[i]) and b < len(pools[j]):
                x, y = pools[i][a], pools[j][b]
                overlap += max(0, min(x[1], y[1]) - max(x[0], y[0]))
                if x[1] < y[1]:
                    a += 1
                else:
                    b += 1
            if overlap >= 150:
                if right.speaker not in left.over:
                    left.over.append(right.speaker)
                if left.speaker not in right.over:
                    right.over.append(left.speaker)


def visible_turns(turns: list[Turn]) -> list[Turn]:
    return [turn for turn in turns if turn.host is None]


def active_turns(turns: list[Turn], ms: int) -> list[Turn]:
    return [t for t in turns if t.is_active(ms)]


def current_turn(turns: list[Turn], ms: int) -> Turn | None:
    """The most recently started active paragraph (including its asides)."""
    return max((t for t in turns if t.host is None and t.is_active(ms)), key=lambda t: t.start_ms, default=None)


def find_matches(turns: list[Turn], query: str) -> list[tuple[int, int]]:
    """(turn index, word index) of every word containing *query*, case-insensitively."""
    needle = query.strip().casefold()
    if not needle:
        return []
    return [(turn.index, i) for turn in turns for i, word in enumerate(turn.words) if needle in word.text.casefold()]


def format_time(ms: int, *, force_hours: bool = False) -> str:
    seconds = max(0, int(ms) // 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours or force_hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
