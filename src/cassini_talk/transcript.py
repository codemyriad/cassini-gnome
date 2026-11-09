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
import heapq
from dataclasses import dataclass, field

from .portable import Word
from .readable import readable_blocks, repair_word_ends
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
    block_positions: list[int] = field(default_factory=list)
    block_bounds: list[tuple[int, int]] = field(default_factory=list)
    continuation: bool = False

    @property
    def crosstalk(self):
        return bool(self.words) and all(word.low_confidence for word in self.words)

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
    # Portable words have no rejected/synthetic timing pool. Like the viewer,
    # retain a block envelope when none of its words has positive timing.
    if spans:
        return spans
    timed = [word for word in words if any(char.isalnum() for char in word.text)] or words
    return [(timed[0].start_ms, timed[-1].end_ms)] if timed else []


def _turn_spans(turn):
    intervals = sorted(
        span for first, last in turn.fragments or [(0, len(turn.words))] for span in _spans(turn.words[first:last])
    )
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _bounds(spans):
    # Keep zero-length fallback envelopes for intervening evidence, but the
    # viewer does not attest a block whose audible envelope has no duration.
    return (spans[0][0], spans[-1][1]) if spans and spans[-1][1] > spans[0][0] else None


def _touches(left, right):
    return any(max(a, c) - min(b, d) <= TOUCH_TOLERANCE_MS for a, b in left for c, d in right)


def gap_break(gaps, first, second):
    """D63 #2: two words of one speaker never share a turn across one of its gap ranges."""
    return bool(gaps) and turn_break(
        own_gap_ranges(gaps, first.speaker), (first.start_ms, first.end_ms), (second.start_ms, second.end_ms)
    )


def build_turns(words: list[Word], gaps: dict | None = None) -> list[Turn]:
    """Viewer readable blocks, timing repair, and interrupted speaker runs.

    Preserve D63 source ownership and the legacy-only quote pairing bridge.
    Group before sorting blocks: file order is part of the portable pipeline.
    """
    blocks = readable_blocks(words, gaps, gap_break)
    repaired = repair_word_ends(words, blocks)
    values = []
    first = 0
    for end in range(1, len(words) + 1):
        if end == len(words) or words[end].speaker != words[first].speaker:
            values.extend(display_items(words[first:end]))
            first = end
    fragments = []
    for indices in blocks:
        chunk = [repaired[i] for i in indices]
        timed = [i for i in indices if any(char.isalnum() for char in words[i].text)] or indices
        start, end = words[timed[0]].start_ms, words[timed[-1]].end_ms
        if any(repaired[i] is not words[i] for i in indices):
            end = max(start, min(end, max(word.end_ms for word in chunk)))
        fragment = Turn(0, chunk[0].speaker, chunk, indices[0], start, end)
        fragment.word_indices = list(indices)
        fragment.display = tuple(values[i] for i in indices)
        fragment.block_bounds = [(start, end)]
        fragments.append(fragment)
    fragments.sort(key=lambda fragment: fragment.start_ms)
    spans = [_spans(fragment.words) for fragment in fragments]
    turns = []
    displays = {}
    opened = {}
    for position, fragment in enumerate(fragments):
        fragment.block_positions = [position]
        if spans[position]:
            fragment.start_ms, fragment.end_ms = spans[position][0][0], spans[position][-1][1]
        previous = opened.get(fragment.speaker)
        join = False
        bounds = _bounds(spans[position])
        if previous and bounds and _bounds(spans[previous[1]]):
            run, last = previous
            # Reject distant seams before consulting any intervening blocks.
            if abs(bounds[0] - run.end_ms) <= SEAM_GAP_MS:
                sides = spans[last] + spans[position]
                occupied = False
                continuous = False
                for i in range(last + 1, position):
                    occupied |= bool(spans[i])
                    if _touches(spans[i], sides):
                        continuous = True
                        break
                join = (not occupied or continuous) and not gap_break(gaps, run.words[-1], fragment.words[0])
        if join:
            start = len(run.words)
            run.words.extend(fragment.words)
            run.word_indices.extend(fragment.word_indices)
            displays[run.index].extend(fragment.display)
            run.fragments.append((start, len(run.words)))
            run.block_positions.extend(fragment.block_positions)
            run.block_bounds.extend(fragment.block_bounds)
            run.end_ms = max(run.end_ms, fragment.end_ms)
        else:
            run = fragment
            run.index = len(turns)
            run.fragments = [(0, len(run.words))]
            turns.append(run)
            displays[run.index] = list(run.display)
        opened[fragment.speaker] = (run, position)
    for run in turns:
        run.display = tuple(displays[run.index])
    _attach_asides(turns)
    _mark_overlap(turns)
    return turns


def _attach_asides(turns: list[Turn]) -> None:
    # Index each host seam once, rather than rescanning all its fragments for
    # every short reply. Suffix minima preserve the viewer's last-block rule
    # even when a producer's block timings are not monotone.
    seams, starts = [], {}
    for host in turns:
        if not _bounds(_turn_spans(host)):
            continue
        fragments = [(a, b, _bounds(_spans(host.words[a:b]))) for a, b in host.fragments]
        starts[host.index] = sorted(bounds[0] for _a, _b, bounds in fragments if bounds)
        following = float("inf")
        for i in range(len(fragments) - 1, -1, -1):
            _a, end, left = fragments[i]
            if left and i + 1 < len(fragments):
                right = fragments[i + 1][2]
                upper = min(following, left[1] + SIDE_GAP_MS)
                if right and left[0] < upper:
                    seams.append((left[0], upper, host.index, end - 1, left[1], right[0]))
            if left:
                following = min(following, left[0])
    seams.sort()
    active, expiry, cursor, candidates = {}, [], 0, {}
    for turn in sorted(turns, key=lambda turn: turn.start_ms):
        pool = _turn_spans(turn)
        if not _bounds(pool) or sum(end - start for start, end in pool) > ASIDE_MAX_MS:
            continue
        while cursor < len(seams) and seams[cursor][0] < turn.start_ms:
            seam = seams[cursor]
            active[cursor] = seam
            heapq.heappush(expiry, (seam[1], cursor))
            cursor += 1
        while expiry and expiry[0][0] < turn.start_ms:
            _end, key = heapq.heappop(expiry)
            active.pop(key, None)
        best = None
        for _lower, _upper, index, after, left_end, right_start in active.values():
            host = turns[index]
            if host.speaker == turn.speaker or turn.end_ms > host.end_ms or host.start_ms > turn.start_ms:
                continue
            if right_start - turn.end_ms > SIDE_GAP_MS:
                continue
            points = starts[index]
            if bisect.bisect_right(points, turn.end_ms) - bisect.bisect_right(points, turn.start_ms) > 1:
                continue
            score = (left_end, host.end_ms - host.start_ms, -host.index)
            if best is None or score > best[0]:
                best = score, host, after
        if best:
            candidates[turn.index] = best
    for index, (_score, host, after) in candidates.items():
        if host.index not in candidates:
            turn = turns[index]
            turn.host = host
            host.asides.append((after, turn))
    for host in turns:
        host.asides.sort(key=lambda pair: pair[1].start_ms)


def _mark_overlap(turns):
    # Sweep actual audible intervals, not long turn envelopes containing
    # thousands of holes. Work tracks spans and real intersections; dense
    # simultaneous speech necessarily has one result per intersecting pair.
    events = sorted((start, end, turn.index) for turn in turns for start, end in _turn_spans(turn) if end > start)
    active, expiry, durations = {}, [], {}
    for serial, (start, end, index) in enumerate(events):
        while expiry and expiry[0][0] <= start:
            _end, key = heapq.heappop(expiry)
            active.pop(key, None)
        left = turns[index]
        for other_end, other_index in active.values():
            right = turns[other_index]
            if left.speaker == right.speaker or left.host is right or right.host is left:
                continue
            pair = (min(index, other_index), max(index, other_index))
            durations[pair] = durations.get(pair, 0) + min(end, other_end) - start
        active[serial] = end, index
        heapq.heappush(expiry, (end, serial))
    stretches = [(duration, a, b) for (a, b), duration in durations.items() if duration >= 150]
    ranks = {turn.index: rank for rank, turn in enumerate(sorted(turns, key=lambda turn: turn.start_ms))}
    seen = [set(turn.over) for turn in turns]
    for _duration, a, b in sorted(
        stretches,
        key=lambda stretch: (
            -stretch[0],
            stretch[1],
            max(ranks[stretch[1]], ranks[stretch[2]]),
            min(ranks[stretch[1]], ranks[stretch[2]]),
        ),
    ):
        left, right = turns[a], turns[b]
        if right.speaker not in seen[a]:
            seen[a].add(right.speaker)
            left.over.append(right.speaker)
        if left.speaker not in seen[b]:
            seen[b].add(left.speaker)
            right.over.append(left.speaker)


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
    matches = []
    for row in visible_turns(turns):
        chips = {}
        for after, aside in row.asides:
            chips.setdefault(after, []).append(aside)
        for i, value in enumerate(row.items):
            if value and needle in value.casefold():
                matches.append((row.index, i))
            for aside in chips.get(i, ()):
                matches.extend(
                    (aside.index, j) for j, shown in enumerate(aside.items) if shown and needle in shown.casefold()
                )
    return matches


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


def speaker_list(names, *, language="en"):
    """Viewer overlap caption: A, B and C, preserving overlap-size order."""
    names = list(dict.fromkeys(names))
    if language != "en":
        # The shared meeting.turn.over catalog specifies comma-separated labels.
        return ", ".join(names)
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1]


def row_text(turn, speaker_name=lambda speaker: speaker):
    """Copy/export/search prose plus exact character offsets of all timed atoms.

    Chip names and parentheses are text too. Retain their widths when mapping
    a Search hit back to its original timed word, including a folded reply.
    """
    parts, spans = [], []
    length = 0

    def append(value, word=None):
        nonlocal length
        if parts:
            parts.append(" ")
            length += 1
        if word is not None:
            spans.append((word, value, length))
        parts.append(value)
        length += len(value)

    chips = {}
    for after, aside in turn.asides:
        chips.setdefault(after, []).append(aside)
    for i, (word, value) in enumerate(zip(turn.words, turn.items, strict=True)):
        if value:
            append(value, word)
        else:
            spans.append((word, value, length))
        for aside in chips.get(i, ()):
            append("(" + speaker_name(aside.speaker) + ":")
            for atom, shown in zip(aside.words, aside.items, strict=True):
                if shown:
                    append(shown, atom)
            parts[-1] += ")"
            length += 1
    return "".join(parts), tuple(spans)
