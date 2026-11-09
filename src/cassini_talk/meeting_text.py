# SPDX-License-Identifier: GPL-3.0-or-later
"""R12: shares from timed items, and timing-preserving transcript markup."""

from dataclasses import dataclass
from fractions import Fraction
from html import escape

from .portable import Word
from .transcript import Turn, build_turns, display_items, join_items, visible_turns

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


def _item_link(word, value, position, uri):
    value = escape(value)
    if word.start_ms <= position < word.end_ms:
        value = f'<span background="#fbe7a1" foreground="#202024">{value}</span>'
    elif word.start_ms > position:
        value = f'<span alpha="60%">{value}</span>'
    return f'<a href="{uri}">{value}</a>'


def item_markup(words, position, *, dark=False, values=None):
    """Markup for timed items; *values* is their displayed text (D38.5)."""
    if values is None:
        values = display_items(words)
    return " ".join(
        _item_link(word, value, position, f"item:{index}")
        for index, (word, value) in enumerate(zip(words, values, strict=True))
        if value
    )


def reading_markup(turn, position, speaker_name, *, dark=False):
    """A bounded paragraph with named, individually seekable interjections."""

    prefixes = {id(turn): "item"}
    prefixes.update((id(aside), f"aside:{i}") for i, (_, aside) in enumerate(turn.asides))
    values = {id(owner): owner.items for owner in [turn] + [aside for _, aside in turn.asides]}

    def link(owner, index):
        return _item_link(owner.words[index], values[id(owner)][index], position, f"{prefixes[id(owner)]}:{index}")

    def chip(aside):
        name = speaker_name(aside.speaker)
        links = " ".join(link(aside, i) for i, value in enumerate(aside.items) if value)
        bg, alpha = ("#ffffff", "10%") if dark else ("#000006", "7%")
        markup = f'<span size="small" background="{bg}" bgalpha="{alpha}"> <b>{escape(name)}:</b> {links} </span>'
        return markup, f" {name}: {aside.text} "

    return join_items(turn, link, chip)[0]


def reading_word(turn, uri):
    """Resolve only links emitted by reading_markup, including folded words."""
    parts = uri.split(":")
    if len(parts) == 2 and parts[0] == "item":
        owner, index = turn, int(parts[1])
    elif len(parts) == 3 and parts[0] == "aside" and 0 <= int(parts[1]) < len(turn.asides):
        owner, index = turn.asides[int(parts[1])][1], int(parts[2])
    else:
        raise ValueError("Unknown transcript link")
    if not 0 <= index < len(owner.words):
        raise ValueError("Unknown transcript item")
    return owner.words[index]


def reading_turns(words, gaps=None):
    """Reconstruct speech before splitting it into bounded reading paragraphs.

    Share the viewer's turn/interjection model with previews and portable files.
    Retain original indices so speaker listening also finds words folded into a
    host paragraph. Quotes are paired before any layout split (D38.5), and the
    speaker's own source gaps still break turns (D63 #2).
    """
    ordered = sorted(enumerate(words), key=lambda item: item[1].start_ms)
    source_indices = [index for index, _word in ordered]
    reconstructed = build_turns([word for _index, word in ordered], gaps=gaps)
    paragraphs = []
    for host in visible_turns(reconstructed):
        first = 0
        while first < len(host.words):
            end = first + 1
            while (
                end < len(host.words)
                and end - first < 100
                and host.words[end].start_ms - host.words[first].start_ms < 30000
            ):
                end += 1
            chunk = host.words[first:end]
            indices = [source_indices[i] for i in host.word_indices[first:end]]
            row = Turn(0, host.speaker, chunk, indices[0], chunk[0].start_ms, max(w.end_ms for w in chunk))
            row.word_indices = indices
            row.display = host.items[first:end]
            for after, aside in host.asides:
                if first <= after < end:
                    aside.word_indices = [source_indices[i] for i in aside.word_indices]
                    aside.first_word = aside.word_indices[0]
                    aside.host = row
                    row.asides.append((after - first, aside))
                    row.end_ms = max(row.end_ms, aside.end_ms)
            paragraphs.append(row)
            first = end
    paragraphs.sort(key=lambda row: row.start_ms)
    for index, row in enumerate(paragraphs):
        row.index = index
    return tuple(paragraphs)
