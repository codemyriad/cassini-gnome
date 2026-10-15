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

# A turn this short that falls inside another speaker's turn is shown inline
# in that turn ("Yeah." while someone else is talking) instead of as its own
# paragraph, which would break the other speaker's sentence in two.
ASIDE_MAX_WORDS = 3
ASIDE_MAX_MS = 2500


@dataclass(slots=True)
class Turn:
    """A maximal run of consecutive items by one speaker, in file order."""

    index: int
    speaker: str
    words: list[Word]
    first_word: int  # index of words[0] in the whole transcript
    start_ms: int
    end_ms: int
    asides: list[tuple[int, Turn]] = field(default_factory=list)  # (after word i, turn)
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


def build_turns(words: list[Word]) -> list[Turn]:
    """Group items into turns. Never sorts: SPEC forbids reordering by time."""
    turns: list[Turn] = []
    for i, word in enumerate(words):
        if turns and turns[-1].speaker == word.speaker:
            turn = turns[-1]
            turn.words.append(word)
            turn.end_ms = max(turn.end_ms, word.end_ms)
        else:
            turns.append(Turn(len(turns), word.speaker, [word], i, word.start_ms, word.end_ms))
    _attach_asides(turns)
    return turns


def _attach_asides(turns: list[Turn]) -> None:
    for prev, turn in zip(turns, turns[1:], strict=False):
        host = prev.host or prev
        if (
            len(turn.words) <= ASIDE_MAX_WORDS
            and turn.end_ms - turn.start_ms <= ASIDE_MAX_MS
            and turn.speaker != host.speaker
            and host.start_ms <= turn.start_ms
            and turn.end_ms <= host.end_ms
            and not turn.asides
        ):
            after = max(0, host.word_at(turn.start_ms))
            host.asides.append((after, turn))
            turn.host = host


def visible_turns(turns: list[Turn], hidden: set[str]) -> list[Turn]:
    """Turns shown as paragraphs. An aside whose host is hidden stands alone."""
    out = []
    for turn in turns:
        if turn.speaker in hidden:
            continue
        if turn.host is not None and turn.host.speaker not in hidden:
            continue
        out.append(turn)
    return out


def active_turns(turns: list[Turn], ms: int) -> list[Turn]:
    return [t for t in turns if t.is_active(ms)]


def current_turn(turns: list[Turn], ms: int, hidden: set[str] = frozenset()) -> Turn | None:
    """The turn to follow at *ms*: the most recently started visible active turn."""
    best = None
    for turn in turns:
        if turn.speaker in hidden or turn.host is not None and turn.host.speaker not in hidden:
            continue
        if turn.is_active(ms) and (best is None or turn.start_ms >= best.start_ms):
            best = turn
    return best


def find_matches(turns: list[Turn], query: str, hidden: set[str] = frozenset()) -> list[tuple[int, int]]:
    """(turn index, word index) of every word containing *query*, case-insensitively."""
    needle = query.strip().casefold()
    if not needle:
        return []
    out = []
    for turn in turns:
        if turn.speaker in hidden:
            continue
        for i, word in enumerate(turn.words):
            if needle in word.text.casefold():
                out.append((turn.index, i))
    return out


def format_time(ms: int, *, force_hours: bool = False) -> str:
    seconds = max(0, int(ms) // 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours or force_hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _vtt_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def export_text(turns: list[Turn], label, title: str | None = None) -> str:
    lines = []
    if title:
        lines += [title, ""]
    for turn in turns:
        lines.append(f"[{format_time(turn.start_ms)}] {label(turn.speaker)}: {turn.text}")
    return "\n".join(lines) + "\n"


def export_webvtt(turns: list[Turn], label, max_words: int = 14) -> str:
    """WebVTT cues of at most *max_words* words, with voice spans for speakers."""
    out = ["WEBVTT", ""]
    for turn in sorted(turns, key=lambda t: t.start_ms):
        for i in range(0, len(turn.words), max_words):
            chunk = turn.words[i : i + max_words]
            start = chunk[0].start_ms
            end = max(max(w.end_ms for w in chunk), start + 1)
            text = " ".join(w.text for w in chunk)
            name = label(turn.speaker).replace(">", "")
            out += [f"{_vtt_time(start)} --> {_vtt_time(end)}", f"<v {name}>{_escape_vtt(text)}", ""]
    return "\n".join(out)


def _escape_vtt(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
