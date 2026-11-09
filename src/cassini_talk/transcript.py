# transcript.py
#
# Copyright 2026 Codemyriad OÜ
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turns, interjections and search over a cassini.words.v1 transcript.

No GTK here: the window turns these into widgets.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .portable import Word
from .source_ownership import own_gap_ranges, turn_break

# How long a turn stays highlighted after its last word, so short gaps
# between words do not make the highlight flicker.
LINGER_MS = 300

# Ported from gocassini’s transcript overlap implementation. Judge whole
# speaker runs, not individual fragments, so competing speech stays separate.
SEAM_GAP_MS = 400
ASIDE_MAX_MS = 2000
SIDE_GAP_MS = 500
TOUCH_TOLERANCE_MS = 20


def quote_items(texts):
    """D38.5 quote pairing; D47 uses this at write time for new transcripts.

    Pairing runs in reading order over everything passed in, which must be one
    whole speaker turn. A final unmatched straight quote is dropped; an item
    left with only whitespace once its quote is dropped displays as "".
    """
    texts = tuple(texts)
    remaining = sum(value.count('"') for value in texts)
    opening = True
    result = []
    for value in texts:
        chars = []
        dropped = False
        for char in value:
            if char == '"':
                # Pair in reading order within this turn; discard a final orphan.
                if remaining > 1 or not opening:
                    chars.append("“" if opening else "”")
                    opening = not opening
                else:
                    dropped = True
                remaining -= 1
            else:
                chars.append(char)
        joined = "".join(chars)
        result.append("" if dropped and not joined.strip() else joined)
    return tuple(result)


def normalize_quote_items(items):
    """D47: store paired quotes once, retaining IDs, timing and additive fields.

    A consecutive speaker run is the pairing boundary. Already normalized
    curly quotes are unchanged, so retry/publication is idempotent. Raw
    recognition items are never mutated (a later partial may complete a pair).
    """
    result = [dict(item) for item in items]
    first = 0
    for end in range(1, len(result) + 1):
        if end == len(result) or result[end]["speaker"] != result[first]["speaker"]:
            for item, value in zip(result[first:end], quote_items(i["text"] for i in result[first:end]), strict=True):
                item["text"] = value
            first = end
    return result


def display_items(words):
    """Read Cassini words verbatim; legacy folders keep their migration bridge."""
    words = tuple(words)
    if all(word.text_is_stored for word in words):
        return tuple(word.text for word in words)
    return quote_items(word.text for word in words)


def turn_text(texts):
    return " ".join(value for value in quote_items(texts) if value)


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
    host: Turn | None = field(default=None, compare=False, repr=False)
    word_indices: list[int] = field(default_factory=list)  # original transcript indices, including after rejoining
    # D38.5 displayed text per item, paired per consecutive speaker run: one
    # run can span several reading paragraphs, and build_turns rejoins runs.
    display: tuple[str, ...] | None = None

    @property
    def items(self) -> tuple[str, ...]:
        """Displayed text of each timed item; "" for an item shown as nothing."""
        if self.display is not None and len(self.display) == len(self.words):
            return self.display
        return display_items(self.words)

    @property
    def text(self) -> str:
        return " ".join(value for value in self.items if value)

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


def gap_break(gaps, first, second):
    """D63 #2: two words of one speaker never share a turn across one of its gap ranges."""
    return bool(gaps) and turn_break(
        own_gap_ranges(gaps, first.speaker), (first.start_ms, first.end_ms), (second.start_ms, second.end_ms)
    )


def build_turns(words: list[Word], gaps: dict | None = None) -> list[Turn]:
    """Rejoin interrupted speaker runs without reordering their words.

    The portable format supplies words rather than Nextcloud's display blocks;
    consecutive same-speaker items are the fragment boundaries available here.
    `gaps` (library.turn_gaps) also ends a fragment, and stops a rejoin, at a
    gap range of the speaker's own tracks (D63 #2).
    """
    fragments, runs = [], []  # runs: fragment slices of each consecutive speaker run
    for i, word in enumerate(words):
        if fragments and fragments[-1].speaker == word.speaker and not gap_break(gaps, words[i - 1], word):
            fragment = fragments[-1]
            fragment.words.append(word)
            fragment.word_indices.append(i)
            fragment.end_ms = max(fragment.end_ms, word.end_ms)
            continue
        if fragments and fragments[-1].speaker == word.speaker:
            runs[-1][1] += 1
        else:
            runs.append([len(fragments), len(fragments) + 1])
        fragments.append(Turn(len(fragments), word.speaker, [word], i, word.start_ms, word.end_ms))
        fragments[-1].word_indices = [i]
    # D38.5 pairs quotes within each consecutive speaker run, like Search,
    # Markdown and the Meeting page; rejoining must never pair across a
    # speaker change, and a gap break never changes the pairing.
    for first, end in runs:
        values = display_items(word for fragment in fragments[first:end] for word in fragment.words)
        for fragment in fragments[first:end]:
            fragment.display, values = values[: len(fragment.words)], values[len(fragment.words) :]
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
                and not gap_break(gaps, run.words[-1], fragment.words[0])
            )
        if join:
            start = len(run.words)
            run.words.extend(fragment.words)
            run.word_indices.extend(fragment.word_indices)
            run.display += fragment.display
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
    fragment_spans = {}
    active = []
    for turn in sorted(turns, key=lambda turn: turn.start_ms):
        active = [host for host in active if host.end_ms >= turn.start_ms]
        hosts = active[:]
        active.append(turn)
        spans = _spans(turn.words)
        if not spans or sum(end - start for start, end in spans) > ASIDE_MAX_MS:
            continue
        best = None
        for host in hosts:
            if host.speaker == turn.speaker or turn.end_ms > host.end_ms:
                continue
            if host.index not in fragment_spans:
                fragment_spans[host.index] = [
                    (start, end, _spans(host.words[start:end])) for start, end in host.fragments
                ]
            fragments = fragment_spans[host.index]
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
    active = []
    for left in sorted(turns, key=lambda turn: turn.start_ms):
        active = [right for right in active if right.end_ms > left.start_ms]
        for right in active:
            if left.speaker == right.speaker:
                continue
            a = b = overlap = 0
            while a < len(pools[left.index]) and b < len(pools[right.index]):
                x, y = pools[left.index][a], pools[right.index][b]
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
        active.append(left)


def visible_turns(turns: list[Turn]) -> list[Turn]:
    return [turn for turn in turns if turn.host is None]


def active_turns(turns: list[Turn], ms: int) -> list[Turn]:
    return [t for t in turns if t.is_active(ms)]


def current_turn(turns: list[Turn], ms: int) -> Turn | None:
    """The most recently started active paragraph (including its asides)."""
    return max((t for t in turns if t.host is None and t.is_active(ms)), key=lambda t: t.start_ms, default=None)


def find_matches(turns: list[Turn], query: str) -> list[tuple[int, int]]:
    """(turn index, word index) of every word whose displayed text contains *query*.

    Matches what the reader shows (D38.5 quotes), so an item displayed as
    nothing is never counted or focused.
    """
    needle = query.strip().casefold()
    if not needle:
        return []
    return [
        (turn.index, i) for turn in turns for i, value in enumerate(turn.items) if value and needle in value.casefold()
    ]


def join_items(turn: Turn, link, chip) -> tuple[str, list[int]]:
    """Markup for *turn* and the UTF-8 byte offset of each item in its text.

    ``link(owner, i)`` is the markup of a displayed item and ``chip(aside)``
    returns an aside's (markup, plain text). Items displayed as "" (D38.5) emit
    nothing and no separator, so offsets match the rendered layout.
    """
    asides = {}
    for after, aside in turn.asides:
        asides.setdefault(after, []).append(aside)
    parts, offsets = [], []
    length = 0
    for i, value in enumerate(turn.items):
        if value:
            if parts:
                parts.append(" ")
                length += 1
            parts.append(link(turn, i))
            offsets.append(length)
            length += len(value.encode())
        else:
            offsets.append(length)
        for aside in asides.get(i, ()):
            markup, plain = chip(aside)
            if parts:
                parts.append(" ")
                length += 1
            parts.append(markup)
            length += len(plain.encode())
    return "".join(parts), offsets


def format_time(ms: int, *, force_hours: bool = False) -> str:
    seconds = max(0, int(ms) // 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours or force_hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
