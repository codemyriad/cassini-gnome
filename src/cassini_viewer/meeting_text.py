# SPDX-License-Identifier: GPL-3.0-or-later
"""R12: shares from timed items, and timing-preserving transcript markup."""

from fractions import Fraction
from html import escape

from .transcript import display_items

COLOURS = ("#1c71d8", "#b34d00", "#813d9c", "#21804b", "#bd2028", "#167c8b")
DARK_COLOURS = ("#78aeed", "#ffa348", "#ce91df", "#77d39a", "#ff9298", "#79c7d1")


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


def reading_turns(words):
    """Bound paragraph work without changing or synthesising any timed item."""
    from .transcript import Turn

    turns = []
    for word in words:
        if (
            not turns
            or turns[-1].speaker != word.speaker
            or len(turns[-1].words) >= 100
            or word.start_ms - turns[-1].start_ms >= 30000
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
