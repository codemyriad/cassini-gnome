# SPDX-License-Identifier: GPL-3.0-or-later
"""R12: shares from timed items, and timing-preserving transcript markup."""

from dataclasses import dataclass, replace
from fractions import Fraction
from html import escape

from .portable import Word
from .speaker_merges import resolve_merges
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
    if word.low_confidence:
        value = f"<i>{value}</i>"
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


def reading_markup(turn, position, speaker_name, *, dark=False, chip_links=False):
    """A bounded paragraph with named, individually seekable interjections."""

    prefixes = {id(turn): "item"}
    prefixes.update((id(aside), f"aside:{i}") for i, (_, aside) in enumerate(turn.asides))
    values = {id(owner): owner.items for owner in [turn] + [aside for _, aside in turn.asides]}

    def link(owner, index):
        return _item_link(owner.words[index], values[id(owner)][index], position, f"{prefixes[id(owner)]}:{index}")

    def chip(aside):
        name = speaker_name(aside.speaker)
        part = isinstance(aside, LayoutChip)
        opening = not part or aside.opens
        closing = not part or aside.closes
        prefix = f"(<b>{escape(name)}:</b> " if opening else ""
        suffix = ")" if closing else ""
        separator = " "
        if chip_links and aside.words:
            target = prefixes[id(aside)] + (":anchor" if part else ":0")

            def hit(value):
                return f'<a href="{target}">{value}</a>' if value else ""

            prefix, suffix, separator = hit(prefix), hit(suffix), hit(separator)
        links = separator.join(link(aside, i) for i, value in enumerate(aside.items) if value)
        bg, alpha = ("#ffffff", "10%") if dark else ("#000006", "7%")
        markup = f'<span size="small" background="{bg}" bgalpha="{alpha}">{prefix}{links}{suffix}</span>'
        plain = (f"({name}: " if opening else "") + aside.text + (")" if closing else "")
        return markup, plain

    return join_items(turn, link, chip)[0]


def reading_word(turn, uri):
    """Resolve only links emitted by reading_markup, including folded words."""
    parts = uri.split(":")
    if len(parts) == 2 and parts[0] == "item":
        owner, index = turn, int(parts[1])
    elif len(parts) == 3 and parts[0] == "aside" and 0 <= int(parts[1]) < len(turn.asides):
        owner = turn.asides[int(parts[1])][1]
        if parts[2] == "anchor" and isinstance(owner, LayoutChip):
            return owner.anchor
        index = int(parts[2])
    else:
        raise ValueError("Unknown transcript link")
    if not 0 <= index < len(owner.words):
        raise ValueError("Unknown transcript item")
    return owner.words[index]


def reading_turns(words, gaps=None):
    """Viewer rows: split only adjacent own blocks, never after a chip.

    Keep every turn's paragraphs together. Original indices survive grouping,
    rejoining, display timing repair and folding, for seeking and listening.
    """
    paragraphs = []
    for host in visible_turns(build_turns(words, gaps=gaps)):
        chips = {after for after, _aside in host.asides}
        edges = [0]
        for i in range(1, len(host.fragments)):
            first = host.fragments[i][0]
            if host.block_positions[i] == host.block_positions[i - 1] + 1 and first - 1 not in chips:
                edges.append(i)
        edges.append(len(host.fragments))
        for part, (begin, end) in enumerate(zip(edges, edges[1:], strict=False)):
            first, last = host.fragments[begin][0], host.fragments[end - 1][1]
            chunk = host.words[first:last]
            indices = host.word_indices[first:last]
            bounds = list(host.block_bounds[begin:end])
            row = Turn(len(paragraphs), host.speaker, chunk, indices[0], 0, 0)
            row.fragments = [(a - first, b - first) for a, b in host.fragments[begin:end]]
            row.word_indices = indices
            row.display = host.items[first:last]
            row.over = list(host.over) if part == 0 else []
            for after, aside in host.asides:
                if first <= after < last:
                    aside.host = row
                    row.asides.append((after - first, aside))
                    bounds.extend(aside.block_bounds)
            row.start_ms = min(start for start, _end in bounds)
            row.end_ms = max(end for _start, end in bounds)
            previous = paragraphs[-1] if paragraphs else None
            row.continuation = bool(
                previous and previous.speaker == row.speaker and row.start_ms - previous.end_ms <= 60000
            )
            paragraphs.append(row)
    return ReadingRows(paragraphs)


def reader_turns(words, gaps=None):
    """The standalone reader's indexable rows, with separately seekable chips.

    The visible rows are exactly reading_turns. Its older link/search protocol
    addresses every owner by list index, so append the chips with unique indices.
    """
    reading = reading_turns(words, gaps)
    rows = ReaderRows(reading)
    for row in tuple(rows):
        for _after, aside in row.asides:
            aside.index = len(rows)
            rows.append(aside)
    rows.layout = getattr(reading, "layout", reading)
    rows.word_keys = {id(word): (owner.index, i) for owner in rows for i, word in enumerate(owner.words)}
    rows.word_rows = {}
    rows.row_owners = {}
    rows.visible_index = {}
    for row in rows.layout:
        owners = set()
        for owner in [row] + [aside for _, aside in row.asides]:
            for word in owner.words:
                key = rows.word_keys[id(word)]
                rows.word_rows[key] = row.index
                owners.add(key[0])
                rows.visible_index.setdefault(key[0], row.index)
        rows.row_owners[row.index] = owners
    return rows


class ReaderRows(list):
    """Semantic search/seek owners and their worker-built bounded GTK rows."""


def load_reader_turns(meeting, transcript_id):
    """Load and project a standalone transcript entirely on its reader worker."""
    from . import portable
    from .library import turn_gaps

    try:
        words = portable.load_words(meeting, transcript_id)
    except portable.CassiniError as error:
        return [], str(error)
    manifest = meeting.manifest
    gaps = turn_gaps((), {}, manifest.get("x")) if manifest else None
    return reader_turns(words, gaps=gaps), None


def row_caption(turn, speaker_name):
    """Only competing stretches appear in the overlap caption, once per turn."""
    from .i18n import gettext as _
    from .i18n import language
    from .strings import text
    from .transcript import speaker_list

    names = speaker_list((speaker_name(speaker) for speaker in turn.over), language=language())
    captions = [text("meeting.turn.over", names=names)] if names else []
    if turn.crosstalk:
        captions.append(_("Probably crosstalk"))
    return " · ".join(captions)


def changed_header_rows(turns, before, after):
    """Rows affected by a header change, independent of GTK.

    Page signatures contain theme, recognized IDs, speaker roster, merge
    overlay and language. A roster/merge change can also rename unknown IDs.
    """
    turns = tuple(turns)
    if before is None or before[0] != after[0] or before[3:] != after[3:]:
        return turns
    old = {s.id: (s.name, s.colour_index, s.provenance) for s in before[2]}
    new = {s.id: (s.name, s.colour_index, s.provenance) for s in after[2]}
    if old.keys() != new.keys():
        return turns
    changed = set(before[1]) ^ set(after[1])
    changed.update(identifier for identifier in new if old[identifier] != new[identifier])
    if not changed:
        return ()
    references = [{turn.speaker, *turn.over, *(aside.speaker for _after, aside in turn.asides)} for turn in turns]
    known = set(new).union(*references)
    entries = after[3]
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                known.update(value for key in ("from", "into") if isinstance(value := entry.get(key), str))
    merges = resolve_merges(entries, known)
    return tuple(
        turn
        for turn, ids in zip(turns, references, strict=True)
        if (ids | {merges.get(speaker, speaker) for speaker in ids}) & changed
    )


def reading_markup_signature(turn, position, speaker_name, *, dark=False):
    """Only changes that affect rendered word/chip markup, not elapsed ticks."""
    owners = [turn, *(aside for _after, aside in turn.asides)]
    states = tuple(
        (word.start_ms <= position < word.end_ms, word.start_ms > position)
        for owner in owners
        for word, value in zip(owner.words, owner.items, strict=True)
        if value
    )
    names = tuple(speaker_name(aside.speaker) for _after, aside in turn.asides)
    return dark, names, states


class FollowRows:
    """First reading row containing a sounding block, including folded chips.

    Index only on transcript replacement; playback is a binary lookup. Row
    order and chronological order are deliberately independent.
    """

    def __init__(self, rows):
        import heapq

        from .transcript import _spans

        events = []
        self.points = {}
        for index, row in enumerate(rows):
            for owner in (row, *(aside for _after, aside in row.asides)):
                for first, last in owner.fragments or [(0, len(owner.words))]:
                    pool = []
                    for start, end in _spans(owner.words[first:last]):
                        if pool and start - pool[-1][1] <= 400:
                            pool[-1] = (pool[-1][0], max(end, pool[-1][1]))
                        else:
                            pool.append((start, end))
                    for start, end in pool:
                        if end == start:
                            self.points[start] = min(index, self.points.get(start, index))
                        if end > start:
                            events.extend(((start, 1, index), (end, -1, index)))
        self.starts, self.indices = [], []
        active, heap, queued = {}, [], set()
        events.sort()
        position = 0
        while position < len(events):
            time = events[position][0]
            while position < len(events) and events[position][0] == time:
                _time, delta, index = events[position]
                active[index] = active.get(index, 0) + delta
                if delta > 0 and index not in queued:
                    heapq.heappush(heap, index)
                    queued.add(index)
                position += 1
            while heap and active[heap[0]] == 0:
                queued.remove(heapq.heappop(heap))
            following = heap[0] if heap else -1
            if not self.indices or following != self.indices[-1]:
                self.starts.append(time)
                self.indices.append(following)

    def at(self, position):
        from bisect import bisect_right

        index = bisect_right(self.starts, position) - 1
        sounding = self.indices[index] if index >= 0 else -1
        point = self.points.get(position, -1)
        return min(sounding, point) if sounding >= 0 and point >= 0 else max(sounding, point)


# GTK measures and parses one label at a time. Keep its work independent of
# the length of a rejoined viewer paragraph.
LAYOUT_WORD_LIMIT = 128


@dataclass(slots=True, kw_only=True)
class LayoutRow(Turn):
    has_more: bool


@dataclass(slots=True, kw_only=True)
class LayoutChip(Turn):
    anchor: Word
    opens: bool
    closes: bool


def _split_layout_row(row, index):
    """Slice a viewer paragraph for GTK without changing its timed atoms."""
    words, values, indices, fragments, asides = [], [], [], [], []
    used = 0
    start_index = index

    def emit(has_more):
        atoms = words + [word for _, aside in asides for word in aside.words]
        return LayoutRow(
            index,
            row.speaker,
            words.copy(),
            indices[0] if indices else asides[0][1].first_word,
            min(word.start_ms for word in atoms),
            max(word.end_ms for word in atoms),
            asides=asides.copy(),
            fragments=fragments.copy(),
            word_indices=indices.copy(),
            display=tuple(values),
            over=row.over if index == start_index else [],
            continuation=row.continuation or index != start_index,
            has_more=has_more,
        )

    def advance():
        nonlocal index, used
        yield emit(True)
        index += 1
        used = 0
        for buffer in (words, values, indices, fragments, asides):
            buffer.clear()

    display = row.items
    source_indices = row.word_indices or list(range(row.first_word, row.first_word + len(row.words)))
    chips = {}
    for after, aside in row.asides:
        chips.setdefault(after, []).append(aside)
    for first, last in row.fragments or [(0, len(row.words))]:
        fragment_start = len(words)
        for i in range(first, last):
            required = 1 + sum(len(aside.words) for aside in chips.get(i, ()))
            if used and used + required > LAYOUT_WORD_LIMIT:
                if fragment_start < len(words):
                    fragments.append((fragment_start, len(words)))
                yield from advance()
                fragment_start = 0
            words.append(row.words[i])
            values.append(display[i])
            indices.append(source_indices[i])
            used += 1
            for aside in chips.get(i, ()):
                count = len(aside.words)
                if used + count <= LAYOUT_WORD_LIMIT:
                    asides.append((len(words) - 1, aside))
                    used += count
                    continue
                # Keep a whole chip together when it fits. A larger chip
                # continues with one opening and closing and the same hit target.
                if fragment_start < len(words):
                    fragments.append((fragment_start, len(words)))
                yield from advance()
                fragment_start = 0
                aside_display = aside.items
                aside_indices = aside.word_indices or list(range(aside.first_word, aside.first_word + count))
                for start in range(0, count, LAYOUT_WORD_LIMIT):
                    if start:
                        yield from advance()
                    end = min(start + LAYOUT_WORD_LIMIT, count)
                    if start == 0 and end == count:
                        part = aside
                    else:
                        part = LayoutChip(
                            aside.index,
                            aside.speaker,
                            aside.words[start:end],
                            aside_indices[start],
                            aside.words[start].start_ms,
                            aside.words[end - 1].end_ms,
                            word_indices=aside_indices[start:end],
                            display=aside_display[start:end],
                            anchor=aside.words[0],
                            opens=start == 0,
                            closes=end == count,
                        )
                    asides.append((-1, part))
                    used = end - start
        if fragment_start < len(words):
            fragments.append((fragment_start, len(words)))
    if used:
        yield emit(False)


def _layout_rows(rows):
    result = []
    for row in rows:
        if len(row.words) + sum(len(aside.words) for _, aside in row.asides) > LAYOUT_WORD_LIMIT:
            result.extend(_split_layout_row(row, len(result)))
        else:
            result.append(row if row.index == len(result) else replace(row, index=len(result)))
    return result


class ReadingRows(tuple):
    """Rows and their worker-built playback lookup share one lifetime."""

    def __new__(cls, rows, *, build_layout=True):
        result = super().__new__(cls, rows)
        result.follow = FollowRows(result)
        if build_layout and any(
            len(row.words) + sum(len(aside.words) for _, aside in row.asides) > LAYOUT_WORD_LIMIT for row in result
        ):
            result.layout = ReadingRows(_layout_rows(result), build_layout=False)
        return result
