# SPDX-License-Identifier: GPL-3.0-or-later
"""R12: shares from timed items, and timing-preserving transcript markup."""

from dataclasses import dataclass
from fractions import Fraction
from html import escape

from .portable import Word
from .transcript import display_items

COLOURS = ("#1c71d8", "#b34d00", "#813d9c", "#21804b", "#bd2028", "#167c8b")
DARK_COLOURS = ("#78aeed", "#ffa348", "#ce91df", "#77d39a", "#ff9298", "#79c7d1")


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """D70 listening span, retaining the original timed words and their indices."""

    index: int
    speaker: str
    words: tuple[Word, ...]
    word_indices: tuple[int, ...]
    start_ms: int
    end_ms: int

    @property
    def text(self):
        return " ".join(value for value in display_items(self.words) if value)


def speaker_turns(words):
    """Maximal speaker runs after merges, independent of reading paragraphs.

    Callers apply the meeting's merge overlay first. Stable chronological order
    includes overlapping asides as their own speaker runs. Neither pauses nor
    the transcript's layout limits split a listening turn. Original word indices
    let the transcript highlight the same words across several reading rows.
    """
    turns = []
    run, indices = [], []
    end = 0

    def append():
        turns.append(SpeakerTurn(len(turns), run[0].speaker, tuple(run), tuple(indices), run[0].start_ms, end))

    for index, word in sorted(enumerate(words), key=lambda item: item[1].start_ms):
        if run and run[-1].speaker != word.speaker:
            append()
            run, indices = [], []
        end = max(end, word.end_ms) if run else word.end_ms
        run.append(word)
        indices.append(index)
    if run:
        append()
    return tuple(turns)


def speaking_shares(words, speaker_ids):
    totals = {speaker: 0 for speaker in speaker_ids}
    for word in words:
        totals[word.speaker] = totals.get(word.speaker, 0) + max(0, word.end_ms - word.start_ms)
    total = sum(totals.values())
    if not total:
        return {speaker: 0 for speaker in speaker_ids}
    exact = {speaker: Fraction(100 * totals[speaker], total) for speaker in speaker_ids}
    result = {speaker: share.numerator // share.denominator for speaker, share in exact.items()}
    order = {speaker: i for i, speaker in enumerate(speaker_ids)}
    for speaker in sorted(speaker_ids, key=lambda s: (-(exact[s] - result[s]), order[s]))[: 100 - sum(result.values())]:
        result[speaker] += 1
    return result


def item_markup(words, position, *, dark=False, values=None):
    """Markup for timed items; *values* is their displayed text (D38.5)."""
    if values is None:
        values = display_items(words)
    parts = []
    for index, (word, value) in enumerate(zip(words, values, strict=True)):
        if not value:
            continue
        value = escape(value)
        if word.start_ms <= position < word.end_ms:
            value = f'<span background="#fbe7a1" foreground="#202024">{value}</span>'
        elif word.start_ms > position:
            value = f'<span alpha="60%">{value}</span>'
        parts.append(f'<a href="item:{index}">{value}</a>')
    return " ".join(parts)


def reading_turns(words, gaps=None):
    """Bound paragraph work without changing or synthesising any timed item.

    A paragraph also ends at a gap range of the speaker's own tracks (D63 #2).
    """
    from .transcript import Turn, gap_break

    turns = []
    for word in words:
        if (
            not turns
            or turns[-1].speaker != word.speaker
            or len(turns[-1].words) >= 100
            or word.start_ms - turns[-1].start_ms >= 30000
            or gap_break(gaps, turns[-1].words[-1], word)
        ):
            turns.append(Turn(len(turns), word.speaker, [word], 0, word.start_ms, word.end_ms))
        else:
            turn = turns[-1]
            turn.words.append(word)
            turn.end_ms = max(turn.end_ms, word.end_ms)
    # D38.5 pairs quotes across the whole speaker turn, as Search and Markdown
    # do, so a paragraph boundary never drops or mispairs a quote.
    start = 0
    for end in range(1, len(turns) + 1):
        if end == len(turns) or turns[end].speaker != turns[start].speaker:
            run = turns[start:end]
            values = display_items(w for turn in run for w in turn.words)
            offset = 0
            for turn in run:
                turn.display = values[offset : offset + len(turn.words)]
                offset += len(turn.words)
            start = end
    return tuple(turns)
